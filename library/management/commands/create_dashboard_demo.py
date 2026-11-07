import os
import secrets
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone


from library.models import (
    Member,
    Note,
    ModerationAction,
    SecurityConcern,
    SecurityEvent,
)

PREFIX = "dashboard_demo_"
MARKER = "[DASHBOARD DEMO]"

PASSWORD = os.environ.get(
    "STUDYSHELF_DEMO_PASSWORD",
   or secrets.token_urlsafe(24)
)


class Command(BaseCommand):
    help = "Create or remove clearly marked dashboard demonstration records."

    def add_arguments(self, parser):
        parser.add_argument("--cleanup", action="store_true", help="Remove only dashboard demo records.")

    def handle(self, *args, **options):
        with transaction.atomic():
            self.cleanup_demo_records()
            self.stdout.write(self.style.WARNING("Dashboard demo data removed."))
            if options["cleanup"]:
                self.stdout.write(self.style.SUCCESS("Demo cleanup complete."))
                return

            moderator = Member.objects.filter(
                role=Member.Role.MODERATOR, is_active=True
            ).order_by("date_joined").first()
            created_moderator = False
            if moderator is None:
                moderator = Member.objects.create_user(
                    username=f"{PREFIX}moderator",
                    email="dashboard_demo_moderator@example.test",
                    password=PASSWORD,
                    display_name="Dashboard Demo Moderator",
                    role=Member.Role.MODERATOR,
                    is_active=True,
                    is_staff=True,
                )
                created_moderator = True

            today = timezone.localdate()
            member_counts = [2, 4, 3, 5, 2, 6, 4]
            resource_counts = [2, 4, 3, 5, 2, 6, 4]
            review_counts = [1, 3, 2, 4, 2, 5, 3]
            ticket_counts = [1, 2, 0, 3, 1, 4, 2]
            demo_members = []
            demo_notes = []

            for day_index in range(7):
                target_day = today - timedelta(days=6 - day_index)
                target_time = timezone.make_aware(datetime.combine(target_day, time(12, 0)))

                for number in range(member_counts[day_index]):
                    member = Member.objects.create_user(
                        username=f"{PREFIX}{day_index}_{number + 1}",
                        email=f"{PREFIX}{day_index}_{number + 1}@example.test",
                        password=PASSWORD,
                        display_name=f"Demo Member {day_index + 1}-{number + 1}",
                        role=Member.Role.USER,
                        is_active=True,
                        is_staff=False,
                    )
                    Member.objects.filter(pk=member.pk).update(date_joined=target_time)
                    demo_members.append(member)

                for number in range(resource_counts[day_index]):
                    owner = demo_members[(day_index + number) % len(demo_members)]
                    status = [Note.Status.APPROVED, Note.Status.PENDING, Note.Status.REJECTED][number % 3]
                    note = Note.objects.create(
                        owner=owner,
                        title=f"{MARKER} Day {day_index + 1} Resource {number + 1}",
                        subject="Dashboard demonstration",
                        description="Synthetic dashboard test record; no file is attached.",
                        status=status,
                        visibility=Note.Visibility.PUBLIC,
                        downloads=(day_index + number) * 2,
                        reviewed_by=moderator if status != Note.Status.PENDING else None,
                        reviewed_at=target_time if status != Note.Status.PENDING else None,
                    )
                    Note.objects.filter(pk=note.pk).update(created_at=target_time)
                    demo_notes.append(note)

                for number in range(review_counts[day_index]):
                    note = demo_notes[(day_index + number) % len(demo_notes)]
                    action = ModerationAction.objects.create(
                        note=note,
                        member=note.owner,
                        admin=moderator,
                        action="approved" if number % 2 == 0 else "rejected",
                        reason=f"{MARKER} Synthetic review activity for dashboard testing.",
                    )
                    ModerationAction.objects.filter(pk=action.pk).update(created_at=target_time)

                for number in range(ticket_counts[day_index]):
                    concern = SecurityConcern.objects.create(
                        member=demo_members[(day_index + number) % len(demo_members)],
                        category="dashboard_demo",
                        subject=f"{MARKER} Demo support ticket {day_index + 1}-{number + 1}",
                        details="Synthetic ticket for moderator dashboard workload testing.",
                        status="reviewing" if number % 2 else "open",
                        assigned_to=moderator,
                    )
                    SecurityConcern.objects.filter(pk=concern.pk).update(created_at=target_time)

                event = SecurityEvent.objects.create(
                    actor=moderator,
                    event="dashboard_demo",
                    detail=f"{MARKER} Synthetic dashboard audit event for day {day_index + 1}.",
                )
                SecurityEvent.objects.filter(pk=event.pk).update(created_at=target_time)

        self.stdout.write(self.style.SUCCESS("Dashboard demonstration data created successfully!"))
        self.stdout.write(f"Demo members: {sum(member_counts)}")
        self.stdout.write(f"Demo resources: {sum(resource_counts)}")
        self.stdout.write(f"Demo moderation actions: {sum(review_counts)}")
        self.stdout.write(f"Demo security tickets: {sum(ticket_counts)}")
        self.stdout.write(f"Demo audit events: 7")
        self.stdout.write(f"Demo moderator: {moderator.username}")
        if created_moderator:
            self.stdout.write(
        	"Demo moderator created. Set STUDYSHELF_DEMO_PASSWORD "
        	"before running the command if you need a known demo password."
    	    )
        self.stdout.write("All demo records are marked and can be removed with --cleanup.")

    def cleanup_demo_records(self):
        SecurityEvent.objects.filter(detail__startswith=MARKER).delete()
        SecurityConcern.objects.filter(subject__startswith=MARKER).delete()
        ModerationAction.objects.filter(reason__startswith=MARKER).delete()
        Note.objects.filter(title__startswith=MARKER).delete()
        Member.objects.filter(username__startswith=PREFIX).delete()
