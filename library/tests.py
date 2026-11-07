from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from .models import Note, Notification, WarningRecord, Suspension

User = get_user_model()

class StudyShelfSecurityTests(TestCase):
    def staff_login(self):
        self.client.force_login(self.staff)
        session = self.client.session
        session["staff_mfa_verified"] = True
        session.save()

    def setUp(self):
        self.member = User.objects.create_user(username="learner", password="Strong-pass-123")
        self.staff = User.objects.create_user(username="moderator", password="Strong-pass-123", is_staff=True, role="moderator", mfa_enabled=True)

    def test_home_and_library_load(self):
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        self.assertEqual(self.client.get(reverse("note_list")).status_code, 200)

    def test_dashboard_requires_login(self):
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)

    def test_pending_note_is_not_public(self):
        note = Note.objects.create(owner=self.member, title="Pending", subject="Python",
            description="Draft", file=SimpleUploadedFile("draft.txt", b"draft"), status="pending")
        self.assertEqual(self.client.get(reverse("note_detail", kwargs={"slug": note.slug})).status_code, 404)

    def test_member_cannot_access_moderator_portal(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("moderator_dashboard")).status_code, 403)

    def test_staff_can_access_moderator_portal(self):
        self.staff_login()
        self.assertEqual(self.client.get(reverse("moderator_dashboard")).status_code, 200)

    def test_private_note_not_visible_to_other_member(self):
        note = Note.objects.create(owner=self.member, title="Private", subject="Python",
            description="Private", file=SimpleUploadedFile("private.txt", b"private"),
            visibility="private", status="approved")
        other = User.objects.create_user(username="other", password="Strong-pass-123")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("note_detail", kwargs={"slug": note.slug})).status_code, 404)

    def test_pending_upload_requires_explicit_moderator_approval(self):
        note = Note.objects.create(owner=self.member, title="Queue item", subject="Django",
            description="For review", file=SimpleUploadedFile("queue.txt", b"hello"),
            status="pending", scan_status="not_configured")
        self.staff_login()
        url = reverse("moderator_approve", kwargs={"pk": note.pk})
        response = self.client.post(url, {})
        note.refresh_from_db()
        self.assertEqual(note.status, "pending")
        response = self.client.post(url, {"acknowledge_unscanned": "yes"})
        note.refresh_from_db()
        self.assertEqual(note.status, "approved")
        self.assertTrue(Notification.objects.filter(member=self.member, kind="success").exists())

    def test_second_warning_creates_seven_day_upload_suspension(self):
        self.staff_login()
        url = reverse("moderator_warn", kwargs={"pk": self.member.pk})
        self.client.post(url, {"reason_choice": "spam"})
        self.client.post(url, {"reason_choice": "spam"})
        self.assertEqual(WarningRecord.objects.filter(member=self.member, is_revoked=False).count(), 2)
        self.assertTrue(Suspension.objects.filter(member=self.member, kind="upload", active=True).exists())

    def test_fifth_warning_suspends_member_account(self):
        self.staff_login()
        url = reverse("moderator_warn", kwargs={"pk": self.member.pk})
        for number in range(5):
            self.client.post(url, {"reason_choice": "community_guidelines"})
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_suspended)
        self.assertFalse(self.member.is_active)


class MemberFeedbackAndConcernTests(TestCase):
    def staff_login(self):
        self.client.force_login(self.staff)
        session = self.client.session
        session["staff_mfa_verified"] = True
        session.save()

    def setUp(self):
        self.member=User.objects.create_user(username="writer",password="Strong-pass-123")
        self.staff=User.objects.create_user(username="staffer",password="Strong-pass-123",is_staff=True,role="moderator",mfa_enabled=True)

    def test_member_can_create_edit_and_delete_own_feedback(self):
        self.client.force_login(self.member)
        response=self.client.post(reverse("feedback_create"),{"title":"A useful platform","message":"The library is easy to use and I like the resource review process.","rating":"4"})
        self.assertEqual(response.status_code,302)
        from .models import Feedback
        entry=Feedback.objects.get(member=self.member)
        self.client.post(reverse("feedback_edit",kwargs={"pk":entry.pk}),{"title":"Updated feedback","message":"The library is easy to use and navigation is clear.","rating":"5"})
        entry.refresh_from_db(); self.assertEqual(entry.title,"Updated feedback")
        self.client.post(reverse("feedback_delete",kwargs={"pk":entry.pk}))
        entry.refresh_from_db(); self.assertTrue(entry.is_deleted)

    def test_moderator_feedback_removal_requires_reason_and_notifies_author(self):
        from .models import Feedback
        entry=Feedback.objects.create(member=self.member,title="My feedback",message="A detailed experience report",rating=4)
        self.staff_login()
        url=reverse("moderator_delete_feedback",kwargs={"pk":entry.pk})
        self.client.post(url,{"reason_choice":"other"})
        entry.refresh_from_db(); self.assertFalse(entry.is_deleted)
        self.client.post(url,{"reason_choice":"off_topic","custom_reason":""})
        entry.refresh_from_db(); self.assertTrue(entry.is_deleted)
        self.assertTrue(Notification.objects.filter(member=self.member,title="Feedback removed").exists())

    def test_member_can_submit_private_security_concern(self):
        from .models import SecurityConcern
        self.client.force_login(self.member)
        self.client.post(reverse("security_concerns"),{"category":"security_warning","subject":"Warning needs review","details":"I received a warning and do not understand what triggered it."})
        self.assertTrue(SecurityConcern.objects.filter(member=self.member,status="open").exists())

    def test_security_concern_can_reference_member_warning(self):
        from .models import SecurityConcern
        warning=WarningRecord.objects.create(member=self.member,issued_by=self.staff,reason="Policy concern")
        self.client.force_login(self.member)
        self.client.post(reverse("security_concerns"),{"category":"security_warning","warning_id":str(warning.pk),"subject":"Please review my warning","details":"I believe this warning was issued in error and would like a review."})
        concern=SecurityConcern.objects.get(member=self.member)
        self.assertEqual(concern.related_warning_id,warning.pk)

class FunctionalRepairTests(TestCase):
    def setUp(self):
        self.member1=User.objects.create_user(username="writer_one",password="Strong-pass-123")
        self.member2=User.objects.create_user(username="writer_two",password="Strong-pass-123")
        self.staff=User.objects.create_user(username="reviewer",password="Strong-pass-123",is_staff=True,role="moderator",mfa_enabled=True)

    def staff_login(self):
        self.client.force_login(self.staff)
        session=self.client.session
        session["staff_mfa_verified"]=True
        session.save()

    def test_profile_page_and_deletion_reverse_route_load(self):
        self.client.force_login(self.member1)
        self.assertEqual(self.client.get(reverse("profile")).status_code,200)
        self.assertTrue(reverse("request_account_deletion"))

    def test_feedback_page_and_reply_route_load(self):
        from .models import Feedback
        feedback=Feedback.objects.create(member=self.member1,title="Good platform",message="The resource library is useful.")
        self.client.force_login(self.member2)
        self.assertEqual(self.client.get(reverse("feedback_list")).status_code,200)
        response=self.client.post(reverse("feedback_reply_create",kwargs={"pk":feedback.pk}),{"body":"I agree with this feedback."})
        self.assertEqual(response.status_code,302)
        self.assertEqual(feedback.replies.count(),1)

    def test_approved_public_resource_is_visible_and_searchable_by_other_members(self):
        note=Note.objects.create(owner=self.member1,title="Book for Python Programming",subject="Programming, Education",description="A guide to Python.",file=SimpleUploadedFile("python.txt",b"python notes"),status="approved",visibility="public")
        self.client.force_login(self.member2)
        response=self.client.get(reverse("note_list"),{"q":"python"})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"Book for Python Programming")
        self.assertEqual(self.client.get(reverse("note_detail",kwargs={"slug":note.slug})).status_code,200)

    def test_member_can_reply_to_existing_open_security_ticket(self):
        from .models import SecurityConcern,SupportMessage
        concern=SecurityConcern.objects.create(member=self.member1,subject="Login concern",details="I noticed an unexpected login.")
        self.client.force_login(self.member1)
        response=self.client.post(reverse("security_concern_reply",kwargs={"pk":concern.pk}),{"body":"I have another detail to add to this case."})
        self.assertEqual(response.status_code,302)
        self.assertEqual(SupportMessage.objects.filter(concern=concern,is_internal=False).count(),1)

    def test_security_ticket_is_not_listed_in_feedback_moderation(self):
        from .models import SecurityConcern,Feedback
        SecurityConcern.objects.create(member=self.member1,subject="Private security issue",details="This is a private concern.")
        Feedback.objects.create(member=self.member1,title="Public feedback",message="This is ordinary platform feedback.")
        self.staff_login()
        response=self.client.get(reverse("moderator_feedback"))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"Public feedback")
        self.assertNotContains(response,"Private security issue")

    def test_moderator_dashboard_has_visual_series(self):
        self.staff_login()
        response=self.client.get(reverse("moderator_dashboard"))
        self.assertEqual(response.status_code,200)
        self.assertIn("upload_series",response.context)
        self.assertEqual(len(response.context["upload_series"]),7)

    def test_moderator_can_open_inline_pdf_preview(self):
        note=Note.objects.create(owner=self.member1,title="Preview PDF",subject="Education",description="Preview test",file=SimpleUploadedFile("preview.pdf",b"%PDF-1.4 test"),status="pending")
        self.staff_login()
        response=self.client.get(reverse("moderator_file_preview",kwargs={"pk":note.pk}))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"PDF preview")
        content=self.client.get(reverse("moderator_file_content",kwargs={"pk":note.pk}))
        self.assertEqual(content.status_code,200)
        self.assertTrue(content["Content-Disposition"].startswith("inline"))
