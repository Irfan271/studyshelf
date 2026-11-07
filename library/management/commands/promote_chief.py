from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.db import transaction
from library.models import SecurityEvent

class Command(BaseCommand):
    help = "Promote an existing Django superuser to the initial StudyShelf Chief Moderator."

    def add_arguments(self, parser):
        parser.add_argument("username")

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.select_for_update().get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError("No account with that username exists.")
        if not user.is_superuser:
            raise CommandError("The account must first be created with createsuperuser.")
        if User.objects.filter(role="chief_moderator").exclude(pk=user.pk).exists():
            raise CommandError("A Chief Moderator is already assigned. Use the owner-controlled succession process.")
        user.role="chief_moderator"
        user.is_staff=True
        user.is_active=True
        user.save(update_fields=["role", "is_staff", "is_active"])
        SecurityEvent.objects.create(actor=user,event="initial_chief_moderator_assigned",detail="Initial Chief Moderator assigned through promote_chief command")
        self.stdout.write(self.style.SUCCESS(f"{user.username} is now the StudyShelf Chief Moderator. MFA enrolment is required at next staff login."))
