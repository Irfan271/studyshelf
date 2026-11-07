from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from django.urls import reverse

class Member(AbstractUser):
    class Role(models.TextChoices):
        USER="user","User"; MODERATOR="moderator","Moderator"; CHIEF="chief_moderator","Chief Moderator"
    role=models.CharField(max_length=20,choices=Role.choices,default=Role.USER,db_index=True)
    mfa_secret=models.CharField(max_length=64,blank=True)
    mfa_enabled=models.BooleanField(default=False)
    must_change_password=models.BooleanField(default=False)
    deletion_requested_at=models.DateTimeField(null=True,blank=True)
    display_name=models.CharField(max_length=90,blank=True)
    bio=models.TextField(max_length=400,blank=True)
    avatar=models.ImageField(upload_to="avatars/",blank=True,null=True)
    is_suspended=models.BooleanField(default=False)
    suspension_reason=models.CharField(max_length=500,blank=True)
    suspended_at=models.DateTimeField(null=True,blank=True)
    def __str__(self): return self.display_name or self.username

class SubjectTag(models.Model):
    name=models.CharField(max_length=60,unique=True)
    slug=models.SlugField(max_length=70,unique=True)
    is_active=models.BooleanField(default=True)
    def __str__(self): return self.name

class Note(models.Model):
    class Visibility(models.TextChoices):
        PUBLIC="public","Public"; PRIVATE="private","Private"
    class Status(models.TextChoices):
        PENDING="pending","Pending review"; APPROVED="approved","Approved"; REJECTED="rejected","Rejected"; REMOVED="removed","Removed by administrator"
    owner=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="notes")
    title=models.CharField(max_length=180); subject=models.CharField(max_length=240); subjects=models.ManyToManyField(SubjectTag,blank=True,related_name="notes"); description=models.TextField()
    file=models.FileField(upload_to="notes/%Y/%m/"); original_filename=models.CharField(max_length=255,blank=True)
    file_size=models.PositiveBigIntegerField(default=0); sha256=models.CharField(max_length=64,blank=True,db_index=True)
    scan_status=models.CharField(max_length=20,default="not_configured"); scan_detail=models.CharField(max_length=500,blank=True)
    visibility=models.CharField(max_length=10,choices=Visibility.choices,default=Visibility.PUBLIC)
    status=models.CharField(max_length=10,choices=Status.choices,default=Status.PENDING,db_index=True)
    downloads=models.PositiveIntegerField(default=0); moderation_reason=models.CharField(max_length=500,blank=True)
    created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)
    reviewed_at=models.DateTimeField(null=True,blank=True)
    reviewed_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="reviewed_notes")
    rejected_file_purged_at=models.DateTimeField(null=True,blank=True)
    slug=models.SlugField(max_length=210,unique=True,blank=True)
    class Meta: ordering=["-created_at"]
    def save(self,*args,**kwargs):
        if not self.slug:
            base=slugify(self.title)[:170] or "study-note"; candidate=base; suffix=2
            while Note.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate=f"{base}-{suffix}"; suffix+=1
            self.slug=candidate
        if self.file and not self.original_filename: self.original_filename=self.file.name.rsplit("/",1)[-1]
        super().save(*args,**kwargs)
    def get_absolute_url(self): return reverse("note_detail",kwargs={"slug":self.slug})
    def __str__(self): return self.title

class Bookmark(models.Model):
    member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="bookmarks")
    note=models.ForeignKey(Note,on_delete=models.CASCADE,related_name="bookmarks")
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: constraints=[models.UniqueConstraint(fields=["member","note"],name="unique_member_note_bookmark")]

class Notification(models.Model):
    member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="notifications")
    title=models.CharField(max_length=160); message=models.TextField(max_length=1000)
    kind=models.CharField(max_length=30,default="info"); target_url=models.CharField(max_length=300,blank=True)
    is_read=models.BooleanField(default=False); created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=["-created_at"]

class WarningRecord(models.Model):
    member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="warnings")
    issued_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,related_name="issued_warnings")
    reason=models.TextField(max_length=1000); note=models.ForeignKey(Note,on_delete=models.SET_NULL,null=True,blank=True)
    class Severity(models.TextChoices):
        MINOR="minor","Minor"; SERIOUS="serious","Serious"
    severity=models.CharField(max_length=10,choices=Severity.choices,default=Severity.MINOR)
    expires_at=models.DateTimeField(null=True,blank=True,db_index=True)
    review_status=models.CharField(max_length=12,default="none",choices=[("none","No challenge"),("pending","Under review"),("upheld","Upheld"),("revoked","Revoked")])
    created_at=models.DateTimeField(auto_now_add=True); is_revoked=models.BooleanField(default=False)
    revoked_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="revoked_warnings")
    revoked_at=models.DateTimeField(null=True,blank=True)

class Suspension(models.Model):
    class Kind(models.TextChoices):
        UPLOAD="upload","Upload suspension"; ACCOUNT="account","Account suspension"
    member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="suspensions")
    kind=models.CharField(max_length=12,choices=Kind.choices); reason=models.CharField(max_length=500)
    starts_at=models.DateTimeField(default=timezone.now); ends_at=models.DateTimeField(null=True,blank=True)
    active=models.BooleanField(default=True); created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="created_suspensions")
    lifted_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="lifted_suspensions")
    lifted_at=models.DateTimeField(null=True,blank=True)
    @property
    def currently_active(self): return self.active and (self.ends_at is None or self.ends_at>timezone.now())

class ModerationAction(models.Model):
    ACTIONS=[("approved","Approved"),("rejected","Rejected"),("deleted","Deleted"),("warning","Warning issued"),("suspended","Suspended"),("reinstated","Reinstated"),("warning_revoked","Warning revoked"),("feedback_removed","Feedback removed")]
    note=models.ForeignKey(Note,on_delete=models.SET_NULL,null=True,blank=True)
    member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="moderation_actions")
    admin=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="moderator_actions")
    action=models.CharField(max_length=20,choices=ACTIONS); reason=models.TextField(blank=True); created_at=models.DateTimeField(auto_now_add=True)

class SecurityEvent(models.Model):
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True)
    event=models.CharField(max_length=80,db_index=True); detail=models.CharField(max_length=500,blank=True)
    ip_address=models.GenericIPAddressField(null=True,blank=True); created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=["-created_at"]

class Feedback(models.Model):
    member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="feedback_entries")
    title=models.CharField(max_length=120)
    message=models.TextField(max_length=2000)
    rating=models.PositiveSmallIntegerField(default=5)
    allow_replies=models.BooleanField(default=True)
    replies_locked=models.BooleanField(default=False)
    visibility=models.CharField(max_length=12,default="published",choices=[("published","Published"),("hidden","Hidden"),("removed","Removed")])
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    is_deleted=models.BooleanField(default=False)
    deleted_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="deleted_feedback_entries")
    deletion_reason=models.CharField(max_length=500,blank=True)
    deleted_at=models.DateTimeField(null=True,blank=True)
    class Meta: ordering=["-created_at"]
    def __str__(self): return self.title

class SecurityConcern(models.Model):
    class Status(models.TextChoices):
        OPEN="open","Open"; REVIEWING="reviewing","Under review"; AWAITING="awaiting_user","Awaiting user"; RESOLVED="resolved","Resolved"; CLOSED="closed","Closed"
    member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="security_concerns")
    category=models.CharField(max_length=50,default="security_warning")
    related_warning=models.ForeignKey(WarningRecord,on_delete=models.SET_NULL,null=True,blank=True,related_name="concerns")
    subject=models.CharField(max_length=140)
    details=models.TextField(max_length=3000)
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.OPEN,db_index=True)
    moderator_note=models.CharField(max_length=1000,blank=True)
    assigned_to=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="assigned_security_concerns")
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta: ordering=["-created_at"]
    def __str__(self): return self.subject


class FeedbackReply(models.Model):
    feedback=models.ForeignKey(Feedback,on_delete=models.CASCADE,related_name="replies")
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,related_name="feedback_replies")
    parent=models.ForeignKey("self",on_delete=models.CASCADE,null=True,blank=True,related_name="children")
    body=models.TextField(max_length=1500)
    is_official=models.BooleanField(default=False)
    staff_author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="official_feedback_replies")
    status=models.CharField(max_length=12,default="published",choices=[("published","Published"),("hidden","Hidden"),("removed","Removed")])
    removal_reason=models.CharField(max_length=500,blank=True)
    created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)
    class Meta: ordering=["created_at"]

class ContentReport(models.Model):
    reporter=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="content_reports")
    feedback=models.ForeignKey(Feedback,on_delete=models.CASCADE,null=True,blank=True,related_name="reports")
    reply=models.ForeignKey(FeedbackReply,on_delete=models.CASCADE,null=True,blank=True,related_name="reports")
    note=models.ForeignKey(Note,on_delete=models.CASCADE,null=True,blank=True,related_name="reports")
    reason=models.CharField(max_length=500); details=models.TextField(max_length=2000,blank=True)
    status=models.CharField(max_length=12,default="open",choices=[("open","Open"),("reviewing","Under review"),("resolved","Resolved"),("dismissed","Dismissed")])
    created_at=models.DateTimeField(auto_now_add=True)

class WarningChallenge(models.Model):
    warning=models.OneToOneField(WarningRecord,on_delete=models.CASCADE,related_name="challenge")
    member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="warning_challenges")
    explanation=models.TextField(max_length=2000)
    status=models.CharField(max_length=12,default="pending",choices=[("pending","Pending"),("upheld","Upheld"),("revoked","Revoked")])
    reviewer=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="warning_reviews")
    decision_reason=models.TextField(max_length=1000,blank=True)
    created_at=models.DateTimeField(auto_now_add=True); decided_at=models.DateTimeField(null=True,blank=True)

class RecoveryRecord(models.Model):
    content_type=models.CharField(max_length=20,choices=[("note","Resource"),("feedback","Feedback"),("reply","Reply")])
    object_id=models.PositiveBigIntegerField(db_index=True)
    owner=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True)
    removed_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="removed_content_records")
    reason=models.CharField(max_length=500)
    removed_at=models.DateTimeField(default=timezone.now)
    retain_until=models.DateTimeField()
    restored_at=models.DateTimeField(null=True,blank=True)
    restored_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="restored_content_records")
    purged_at=models.DateTimeField(null=True,blank=True)
    purged_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="purged_content_records")
    class Meta: ordering=["-removed_at"]


class SupportMessage(models.Model):
    concern=models.ForeignKey(SecurityConcern,on_delete=models.CASCADE,related_name="messages")
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,related_name="support_messages")
    body=models.TextField(max_length=3000)
    is_internal=models.BooleanField(default=False)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=["created_at"]

class AccountDeletionRequest(models.Model):
    member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="deletion_requests")
    reason=models.TextField(max_length=1000,blank=True)
    status=models.CharField(max_length=12,default="pending",choices=[("pending","Pending"),("approved","Approved"),("rejected","Rejected")])
    reviewed_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="reviewed_deletion_requests")
    created_at=models.DateTimeField(auto_now_add=True); reviewed_at=models.DateTimeField(null=True,blank=True)
