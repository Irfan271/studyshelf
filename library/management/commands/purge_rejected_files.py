from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from library.models import Note

class Command(BaseCommand):
    help = "Purge rejected upload bytes after retention while keeping moderation history."
    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=settings.REJECTED_FILE_RETENTION_DAYS)
        purged = 0
        for note in Note.objects.filter(status=Note.Status.REJECTED, reviewed_at__lt=cutoff).exclude(file=""):
            if note.file:
                note.file.delete(save=False)
            note.file = ""
            note.rejected_file_purged_at = timezone.now()
            note.save(update_fields=["file", "rejected_file_purged_at", "updated_at"])
            purged += 1
        self.stdout.write(self.style.SUCCESS(f"Purged {purged} rejected file(s); moderation history retained."))
