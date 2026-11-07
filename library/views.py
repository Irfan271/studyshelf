from datetime import timedelta
from pathlib import Path
import mimetypes
import csv
import io
import zipfile
import xml.etree.ElementTree as ET

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.crypto import get_random_string
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, F, Count
from django.http import (
    FileResponse,
    Http404,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
    HttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import RegisterForm, ProfileForm, NoteForm, NoteUpdateForm
from .models import (
    Note,
    Bookmark,
    Notification,
    WarningRecord,
    Suspension,
    ModerationAction,
    SecurityEvent,
    Member,
    Feedback,
    SecurityConcern,
    FeedbackReply,
    ContentReport,
    WarningChallenge,
    RecoveryRecord,
    SupportMessage,
    SubjectTag,
)
from .validators import sha256_upload, clamav_scan
from .security import (
    admin_required,
    chief_required,
    rate_limit,
    record_event,
    active_upload_suspension,
)
from .mfa import encrypt_secret, decrypt_secret

import pyotp


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------

def notify(member, title, message, kind="info", target_url=""):
    Notification.objects.create(
        member=member,
        title=title,
        message=message,
        kind=kind,
        target_url=target_url,
    )


def public_notes():
    return Note.objects.filter(
        status=Note.Status.APPROVED,
        visibility=Note.Visibility.PUBLIC,
    ).select_related("owner")


def resource_search(queryset, query):
    """
    Apply the shared resource search fields to member and moderator
    resource listings.

    The search covers resource metadata, subject tags, filenames,
    and contributor account information.
    """
    if not query:
        return queryset

    return queryset.filter(
        Q(title__icontains=query)
        | Q(subject__icontains=query)
        | Q(subjects__name__icontains=query)
        | Q(description__icontains=query)
        | Q(original_filename__icontains=query)
        | Q(owner__username__icontains=query)
        | Q(owner__email__icontains=query)
    ).distinct()


# ---------------------------------------------------------------------
# Public pages and resource search
# ---------------------------------------------------------------------

def home(request):
    featured = public_notes()[:6]

    return render(
        request,
        "library/home.html",
        {
            "featured": featured,
            "total_notes": public_notes().count(),
        },
    )


@rate_limit(40, 60, "library-search", methods=("GET",))
def note_list(request):
    notes = public_notes()

    query = request.GET.get("q", "").strip()[:100]
    subject = request.GET.get("subject", "").strip()[:80]

    notes = resource_search(notes, query)

    if subject:
        notes = notes.filter(
            Q(subject__iexact=subject)
            | Q(subjects__name__iexact=subject)
        ).distinct()

    subject_names = set(
        SubjectTag.objects.filter(is_active=True).values_list(
            "name",
            flat=True,
        )
    )

    for legacy in public_notes().values_list(
        "subject",
        flat=True,
    ).distinct():
        subject_names.update(
            part.strip()
            for part in (legacy or "").split(",")
            if part.strip()
        )

    subjects = sorted(subject_names, key=str.casefold)

    page = Paginator(notes, 12).get_page(request.GET.get("page"))

    return render(
        request,
        "library/note_list.html",
        {
            "notes": page.object_list,
            "page_obj": page,
            "query": query,
            "subject": subject,
            "subjects": subjects,
            "total_results": page.paginator.count,
        },
    )


def note_detail(request, slug):
    note = get_object_or_404(
        Note.objects.select_related("owner"),
        slug=slug,
    )

    if note.status != Note.Status.APPROVED:
        if (
            not request.user.is_authenticated
            or (
                not request.user.is_staff
                and note.owner_id != request.user.id
            )
        ):
            raise Http404

    if note.visibility == "private":
        if (
            not request.user.is_authenticated
            or note.owner_id != request.user.id
        ):
            raise Http404

    bookmarked = (
        request.user.is_authenticated
        and Bookmark.objects.filter(
            member=request.user,
            note=note,
        ).exists()
    )

    return render(
        request,
        "library/note_detail.html",
        {
            "note": note,
            "bookmarked": bookmarked,
        },
    )


@login_required
@rate_limit(30, 60, "download", methods=("GET",))
def download_note(request, slug):
    note = get_object_or_404(Note, slug=slug)

    allowed = (
        note.status == Note.Status.APPROVED
        and note.visibility == Note.Visibility.PUBLIC
    )

    owner_private = (
        note.owner_id == request.user.id
        and note.visibility == Note.Visibility.PRIVATE
        and note.status == Note.Status.APPROVED
    )

    staff_access = (
        request.user.is_staff
        and note.status in [
            Note.Status.PENDING,
            Note.Status.REJECTED,
            Note.Status.APPROVED,
        ]
    )

    if not (allowed or owner_private or staff_access):
        raise Http404

    if not note.file or not Path(note.file.path).exists():
        raise Http404

    Note.objects.filter(pk=note.pk).update(
        downloads=F("downloads") + 1
    )

    response = FileResponse(
        note.file.open("rb"),
        as_attachment=True,
        filename=Path(
            note.original_filename or note.file.name
        ).name,
    )

    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Type"] = "application/octet-stream"

    return response


# ---------------------------------------------------------------------
# Registration and member dashboard
# ---------------------------------------------------------------------

@rate_limit(5, 3600, "register")
def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = RegisterForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)

        messages.success(
            request,
            "Welcome to StudyShelf. Your account is ready.",
        )

        return redirect("dashboard")

    return render(
        request,
        "registration/register.html",
        {"form": form},
    )


@login_required
def dashboard(request):
    if request.user.is_staff:
        return redirect(
            "chief_dashboard"
            if request.user.role == Member.Role.CHIEF
            else "moderator_dashboard"
        )

    mine = Note.objects.filter(owner=request.user)

    bookmarks = Bookmark.objects.filter(
        member=request.user,
        note__status=Note.Status.APPROVED,
    ).select_related("note", "note__owner")

    upload_suspended = active_upload_suspension(request.user)

    warning_count = (
        WarningRecord.objects.filter(
            member=request.user,
            is_revoked=False,
        )
        .filter(
            Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        )
        .count()
    )

    return render(
        request,
        "library/dashboard.html",
        {
            "my_notes": mine[:6],
            "bookmarks": bookmarks[:6],
            "note_count": mine.count(),
            "download_count": sum(
                mine.values_list("downloads", flat=True)
            ),
            "bookmark_count": bookmarks.count(),
            "upload_suspended": upload_suspended,
            "warning_count": warning_count,
        },
    )


# ---------------------------------------------------------------------
# Resource submission and editing
# ---------------------------------------------------------------------

@login_required
def note_create(request):
    if request.user.is_staff:
        return redirect("moderator_dashboard")

    if request.user.is_suspended or not request.user.is_active:
        messages.error(
            request,
            "Your account is suspended. Uploading is unavailable.",
        )
        return redirect("dashboard")

    if active_upload_suspension(request.user):
        messages.error(
            request,
            "Your upload privileges are temporarily suspended.",
        )
        return redirect("dashboard")

    form = NoteForm(
        request.POST or None,
        request.FILES or None,
    )

    if request.method == "POST" and form.is_valid():
        upload = request.FILES["file"]

        digest = sha256_upload(upload)
        scan_status, scan_detail = clamav_scan(upload)

        note = form.save(commit=False)
        note.owner = request.user
        note.original_filename = Path(upload.name).name
        note.file_size = upload.size
        note.sha256 = digest
        note.scan_status = scan_status
        note.scan_detail = scan_detail
        note.status = Note.Status.PENDING

        if scan_status == "infected":
            messages.error(
                request,
                "The file failed the malware scan and was not submitted for approval.",
            )

            record_event(
                request,
                "infected_upload_blocked",
                note.original_filename,
            )

            return render(
                request,
                "library/note_form.html",
                {
                    "form": form,
                    "heading": "Share a resource",
                    "button": "Submit for review",
                },
            )

        note.save()
        form.save_subjects(note)

        notify(
            request.user,
            "Resource submitted",
            "Your resource is in the security review queue. It will not appear publicly until an administrator approves it.",
            "pending",
            note.get_absolute_url(),
        )

        record_event(
            request,
            "note_submitted",
            f"note_id={note.pk}",
        )

        messages.success(
            request,
            "Resource submitted. It will be published only after administrator approval.",
        )

        return redirect("dashboard")

    return render(
        request,
        "library/note_form.html",
        {
            "form": form,
            "heading": "Share a resource",
            "button": "Submit for review",
        },
    )


@login_required
def note_edit(request, slug):
    note = get_object_or_404(
        Note,
        slug=slug,
        owner=request.user,
    )

    if note.status in [
        Note.Status.REJECTED,
        Note.Status.REMOVED,
    ]:
        messages.error(
            request,
            "This resource cannot be edited. Submit a new version instead.",
        )
        return redirect("dashboard")

    form = NoteUpdateForm(
        request.POST or None,
        request.FILES or None,
        instance=note,
    )

    if request.method == "POST" and form.is_valid():
        old_file_name = note.file.name

        updated = form.save(commit=False)

        if request.FILES.get("file"):
            upload = request.FILES["file"]
            scan_status, scan_detail = clamav_scan(upload)

            if scan_status == "infected":
                messages.error(
                    request,
                    "The replacement file failed malware scanning.",
                )
                return redirect("dashboard")

            updated.original_filename = Path(upload.name).name
            updated.file_size = upload.size
            updated.sha256 = sha256_upload(upload)
            updated.scan_status = scan_status
            updated.scan_detail = scan_detail

        updated.status = Note.Status.PENDING
        updated.reviewed_at = None
        updated.reviewed_by = None

        updated.save()
        form.save_subjects(updated)

        if (
            request.FILES.get("file")
            and old_file_name
            and old_file_name != updated.file.name
        ):
            note.file.storage.delete(old_file_name)

        notify(
            request.user,
            "Resource resubmitted",
            "Your edited resource has returned to the administrator review queue.",
            "pending",
            note.get_absolute_url(),
        )

        messages.success(
            request,
            "Changes submitted for administrator review.",
        )

        return redirect("dashboard")

    return render(
        request,
        "library/note_form.html",
        {
            "form": form,
            "heading": "Edit resource",
            "button": "Resubmit for review",
        },
    )


@login_required
@require_POST
def note_delete(request, slug):
    note = get_object_or_404(
        Note,
        slug=slug,
        owner=request.user,
    )

    if note.file:
        note.file.delete(save=False)

    note.delete()

    messages.success(
        request,
        "Your resource was deleted.",
    )

    return redirect("dashboard")


@login_required
@require_POST
def toggle_bookmark(request, slug):
    note = get_object_or_404(
        public_notes(),
        slug=slug,
    )

    bookmark, created = Bookmark.objects.get_or_create(
        member=request.user,
        note=note,
    )

    if not created:
        bookmark.delete()
        messages.info(
            request,
            "Removed from your saved resources.",
        )
    else:
        messages.success(
            request,
            "Saved to your reading list.",
        )

    return redirect(note.get_absolute_url())


# ---------------------------------------------------------------------
# Profile, avatar and notifications
# ---------------------------------------------------------------------

@login_required
def member_avatar(request, pk):
    member = get_object_or_404(Member, pk=pk)

    if not member.avatar:
        raise Http404

    content_type = (
        mimetypes.guess_type(member.avatar.name)[0]
        or "application/octet-stream"
    )

    if not content_type.startswith("image/"):
        raise Http404

    try:
        avatar_file = member.avatar.open("rb")
    except OSError:
        raise Http404

    response = FileResponse(
        avatar_file,
        content_type=content_type,
    )

    response["Content-Disposition"] = "inline"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=300"

    return response


@login_required
def profile(request):
    form = ProfileForm(
        request.POST or None,
        request.FILES or None,
        instance=request.user,
    )

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile updated.")
        return redirect("profile")

    return render(
        request,
        "library/profile.html",
        {"form": form},
    )


@login_required
def notification_list(request):
    notifications = request.user.notifications.all()[:100]

    return render(
        request,
        "library/notifications.html",
        {"notifications": notifications},
    )


@login_required
@require_POST
def notification_read(request, pk):
    notification = get_object_or_404(
        Notification,
        pk=pk,
        member=request.user,
    )

    notification.is_read = True
    notification.save(update_fields=["is_read"])

    if (
        notification.target_url.startswith("/")
        and not notification.target_url.startswith("//")
    ):
        return redirect(notification.target_url)

    return redirect("notifications")


@login_required
@require_POST
def notifications_read_all(request):
    request.user.notifications.filter(
        is_read=False
    ).update(is_read=True)

    return redirect("notifications")


# ---------------------------------------------------------------------
# Moderator dashboard helpers
# ---------------------------------------------------------------------

def _last_seven_day_series(queryset, date_field="created_at"):
    today = timezone.localdate()
    rows = []

    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)

        rows.append(
            {
                "label": day.strftime("%a"),
                "date": day,
                "count": queryset.filter(
                    **{f"{date_field}__date": day}
                ).count(),
            }
        )

    maximum = max(
        [row["count"] for row in rows] or [0]
    ) or 1

    for row in rows:
        row["height"] = max(
            4,
            int(row["count"] * 100 / maximum),
        )

    return rows


# ---------------------------------------------------------------------
# Moderator dashboard
# ---------------------------------------------------------------------

@admin_required
def moderator_dashboard(request):
    pending = Note.objects.filter(status="pending")

    concerns = SecurityConcern.objects.filter(
        status__in=[
            "open",
            "reviewing",
            "awaiting_user",
        ]
    )

    active_reports = ContentReport.objects.filter(
        status__in=["open", "reviewing"]
    )

    feedback_replies = FeedbackReply.objects.filter(
        status="published",
    )

    review_actions = ModerationAction.objects.filter(
        action__in=["approved", "rejected"]
    )

    context = {
        "pending_count": pending.count(),
        "approved_count": Note.objects.filter(
            status="approved"
        ).count(),
        "rejected_count": Note.objects.filter(
            status="rejected"
        ).count(),

        "users_count": Member.objects.filter(
            is_staff=False
        ).count(),

        "warnings_count": WarningRecord.objects.filter(
            is_revoked=False
        ).count(),

        "suspended_count": Member.objects.filter(
            is_suspended=True
        ).count(),

        "feedback_count": Feedback.objects.filter(
            is_deleted=False,
            visibility="published",
        ).count(),

        "feedback_reply_count": feedback_replies.count(),

        "hidden_reply_count": FeedbackReply.objects.filter(
            status="hidden"
        ).count(),

        "removed_reply_count": FeedbackReply.objects.filter(
            status="removed"
        ).count(),

        "open_concerns_count": concerns.count(),
        "open_reports_count": active_reports.count(),

        "recent_events": SecurityEvent.objects.select_related(
            "actor"
        ).order_by("-created_at")[:12],

        # Moderator workspace charts. Keep the data scoped to this
        # moderator where an assignment/decision owner is available.
        "upload_series": _last_seven_day_series(Note.objects.all()),
        "review_series": _last_seven_day_series(review_actions),
        "ticket_series": _last_seven_day_series(concerns),
        "assigned_ticket_series": _last_seven_day_series(concerns.filter(assigned_to=request.user)),

        "assigned_ticket_count": concerns.filter(
            assigned_to=request.user
        ).count(),

        "assigned_ticket_series": _last_seven_day_series(
            concerns.filter(assigned_to=request.user)
        ),

        "recent_review_actions": ModerationAction.objects.select_related(
            "admin",
            "member",
            "note",
        ).order_by("-created_at")[:10],
    }

    return render(
        request,
        "moderator/dashboard.html",
        context,
    )


# ---------------------------------------------------------------------
# Moderator resource search and review queue
# ---------------------------------------------------------------------

@admin_required
def moderator_queue(request):
    query = request.GET.get("q", "").strip()[:100]

    notes = Note.objects.filter(
        status="pending"
    ).select_related("owner")

    notes = resource_search(notes, query)

    page = Paginator(notes, 15).get_page(
        request.GET.get("page")
    )

    return render(
        request,
        "moderator/queue.html",
        {
            "notes": page.object_list,
            "page_obj": page,
            "query": query,
            "total_results": page.paginator.count,
        },
    )


@admin_required
def moderator_files(request):
    status = request.GET.get("status", "approved")

    allowed_statuses = [
        "approved",
        "rejected",
        "removed",
        "pending",
    ]

    if status not in allowed_statuses:
        status = "approved"

    query = request.GET.get("q", "").strip()[:100]

    notes = Note.objects.filter(
        status=status
    ).select_related(
        "owner",
        "reviewed_by",
    )

    notes = resource_search(notes, query)

    page = Paginator(notes, 20).get_page(
        request.GET.get("page")
    )

    return render(
        request,
        "moderator/files.html",
        {
            "notes": page.object_list,
            "page_obj": page,
            "status": status,
            "query": query,
            "total_results": page.paginator.count,
        },
    )


# ---------------------------------------------------------------------
# Resource approval and rejection
# ---------------------------------------------------------------------

@admin_required
@require_POST
def moderator_approve(request, pk):
    with transaction.atomic():
        note = get_object_or_404(
            Note.objects.select_for_update(),
            pk=pk,
            status="pending",
        )

        if note.scan_status == "infected":
            messages.error(
                request,
                "An infected file cannot be approved.",
            )
            return redirect("moderator_queue")

        if note.scan_status not in [
            "clean",
            "not_configured",
            "error",
        ]:
            messages.error(
                request,
                "Unknown scan state. Approval is blocked.",
            )
            return redirect("moderator_queue")

        if note.scan_status == "error":
            messages.error(
                request,
                "The scanner returned an error. Resolve scanning before approval.",
            )
            return redirect("moderator_queue")

        if (
            note.scan_status == "not_configured"
            and request.POST.get("acknowledge_unscanned") != "yes"
        ):
            messages.error(
                request,
                "Antivirus is not configured. Explicitly acknowledge the unscanned file to approve it.",
            )
            return redirect("moderator_queue")

        note.status = "approved"
        note.reviewed_by = request.user
        note.reviewed_at = timezone.now()
        note.moderation_reason = ""

        note.save(
            update_fields=[
                "status",
                "reviewed_by",
                "reviewed_at",
                "moderation_reason",
                "updated_at",
            ]
        )

        ModerationAction.objects.create(
            note=note,
            member=note.owner,
            admin=request.user,
            action="approved",
            reason="Approved after moderator review.",
        )

        notify(
            note.owner,
            "Resource approved",
            f'Your resource "{note.title}" has been approved and is now visible in the StudyShelf library.',
            "success",
            note.get_absolute_url(),
        )

        record_event(
            request,
            "note_approved",
            f"note_id={note.pk}",
        )

    messages.success(
        request,
        "Resource approved and published.",
    )

    return redirect("moderator_queue")


@admin_required
@require_POST
def moderator_reject(request, pk):
    reason = request.POST.get("reason", "").strip()[:500]

    if not reason:
        messages.error(
            request,
            "A rejection reason is required.",
        )
        return redirect("moderator_queue")

    note = get_object_or_404(
        Note,
        pk=pk,
        status="pending",
    )

    note.status = "rejected"
    note.reviewed_by = request.user
    note.reviewed_at = timezone.now()
    note.moderation_reason = reason

    note.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "moderation_reason",
            "updated_at",
        ]
    )

    ModerationAction.objects.create(
        note=note,
        member=note.owner,
        admin=request.user,
        action="rejected",
        reason=reason,
    )

    notify(
        note.owner,
        "Resource not approved",
        f'Your resource "{note.title}" was not approved. Reason: {reason}. The file will be retained privately for up to 30 days.',
        "warning",
    )

    record_event(
        request,
        "note_rejected",
        f"note_id={note.pk}; reason={reason}",
    )

    messages.success(
        request,
        "Resource rejected and kept in private retention storage.",
    )

    return redirect("moderator_queue")


# ---------------------------------------------------------------------
# Member management
# ---------------------------------------------------------------------

@admin_required
def moderator_users(request):
    query = request.GET.get("q", "").strip()[:80]

    users = (
        Member.objects.filter(is_staff=False)
        .annotate(
            active_warning_count=Count(
                "warnings",
                filter=(
                    Q(warnings__is_revoked=False)
                    & (
                        Q(warnings__expires_at__isnull=True)
                        | Q(warnings__expires_at__gt=timezone.now())
                    )
                ),
            )
        )
        .order_by("-date_joined")
    )

    if query:
        users = users.filter(
            Q(username__icontains=query)
            | Q(email__icontains=query)
            | Q(display_name__icontains=query)
        )

    page = Paginator(users, 20).get_page(
        request.GET.get("page")
    )

    return render(
        request,
        "moderator/users.html",
        {
            "users": page.object_list,
            "page_obj": page,
            "query": query,
            "warning_reasons": WARNING_REASONS,
        },
    )


@admin_required
@require_POST
def moderator_warn(request, pk):
    reason = resolve_reason(
        request.POST,
        WARNING_REASONS,
    )[:1000]

    if not reason:
        messages.error(
            request,
            "Choose a reason or enter a custom reason.",
        )
        return redirect("moderator_users")

    with transaction.atomic():
        member = get_object_or_404(
            Member.objects.select_for_update(),
            pk=pk,
            is_staff=False,
        )

        serious_keys = {
            "unsafe_file",
            "harassment",
            "repeated_misuse",
            "copyright",
        }

        severity = (
            "serious"
            if (
                request.POST.get("severity") == "serious"
                or request.POST.get("reason_choice") in serious_keys
            )
            else "minor"
        )

        warning = WarningRecord.objects.create(
            member=member,
            issued_by=request.user,
            reason=reason,
            severity=severity,
            expires_at=(
                None
                if severity == "serious"
                else timezone.now() + timedelta(days=21)
            ),
        )

        count = (
            WarningRecord.objects.filter(
                member=member,
                is_revoked=False,
            )
            .filter(
                Q(expires_at__isnull=True)
                | Q(expires_at__gt=timezone.now())
            )
            .count()
        )

        ModerationAction.objects.create(
            member=member,
            admin=request.user,
            action="warning",
            reason=reason,
        )

        notify(
            member,
            "Warning issued",
            f"You received warning {count} of 5. Reason: {reason}",
            "warning",
        )

        if count == 2:
            Suspension.objects.create(
                member=member,
                kind="upload",
                reason="Automatic 7-day upload suspension after 2 warnings.",
                ends_at=timezone.now() + timedelta(days=7),
                created_by=request.user,
            )

            notify(
                member,
                "Upload access suspended",
                "You have reached 2 warnings. Uploading is disabled for 7 days. Existing approved resources remain available unless separately moderated.",
                "warning",
            )

            ModerationAction.objects.create(
                member=member,
                admin=request.user,
                action="suspended",
                reason="Automatic seven-day upload suspension at warning 2.",
            )

        if count >= 5:
            member.is_suspended = True
            member.is_active = False
            member.suspended_at = timezone.now()
            member.suspension_reason = (
                "Account suspended after reaching five active warnings."
            )

            member.save(
                update_fields=[
                    "is_suspended",
                    "is_active",
                    "suspended_at",
                    "suspension_reason",
                ]
            )

            Suspension.objects.create(
                member=member,
                kind="account",
                reason=member.suspension_reason,
                created_by=request.user,
            )

            notify(
                member,
                "Account suspended",
                "Your account has been suspended after reaching five warnings. Contact the StudyShelf administrator to appeal.",
                "danger",
            )

            ModerationAction.objects.create(
                member=member,
                admin=request.user,
                action="suspended",
                reason=member.suspension_reason,
            )

        record_event(
            request,
            "warning_issued",
            f"member_id={member.pk}; count={count}",
        )

    messages.success(
        request,
        f"Warning issued. Active warning count: {count}.",
    )

    return redirect("moderator_users")


@admin_required
@require_POST
def moderator_revoke_warning(request, pk):
    warning = get_object_or_404(
        WarningRecord.objects.select_related("member"),
        pk=pk,
        is_revoked=False,
    )

    warning.is_revoked = True
    warning.revoked_by = request.user
    warning.revoked_at = timezone.now()

    warning.save(
        update_fields=[
            "is_revoked",
            "revoked_by",
            "revoked_at",
        ]
    )

    member = warning.member

    active_count = (
        WarningRecord.objects.filter(
            member=member,
            is_revoked=False,
        )
        .filter(
            Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        )
        .count()
    )

    ModerationAction.objects.create(
        member=member,
        admin=request.user,
        action="warning_revoked",
        reason=(
            f"Warning #{warning.pk} revoked. "
            f"{request.POST.get('reason', '')}"
        )[:1000],
    )

    if active_count < 2:
        Suspension.objects.filter(
            member=member,
            kind="upload",
            active=True,
            reason="Automatic 7-day upload suspension after 2 warnings.",
        ).update(
            active=False,
            lifted_by=request.user,
            lifted_at=timezone.now(),
        )

    notify(
        member,
        "Warning removed",
        "An administrator revoked one of your warnings. Your active warning count is now "
        + str(active_count)
        + ".",
        "success",
    )

    record_event(
        request,
        "warning_revoked",
        f"warning_id={warning.pk}; member_id={member.pk}",
    )

    messages.success(
        request,
        "Warning revoked and audit record updated.",
    )

    return redirect("moderator_users")


@admin_required
@require_POST
def moderator_upload_suspend(request, pk):
    reason = request.POST.get(
        "reason",
        "Manual administrator upload restriction.",
    )[:500]

    days = (
        int(request.POST.get("days", "7"))
        if request.POST.get("days", "7").isdigit()
        else 7
    )

    days = max(1, min(days, 90))

    member = get_object_or_404(
        Member,
        pk=pk,
        is_staff=False,
    )

    Suspension.objects.create(
        member=member,
        kind="upload",
        reason=reason,
        ends_at=timezone.now() + timedelta(days=days),
        created_by=request.user,
    )

    notify(
        member,
        "Upload access restricted",
        f"An administrator restricted uploads for {days} days. Reason: {reason}",
        "warning",
    )

    record_event(
        request,
        "manual_upload_suspension",
        f"member_id={member.pk}; days={days}",
    )

    messages.success(
        request,
        "Temporary upload suspension applied.",
    )

    return redirect("moderator_users")


@admin_required
@require_POST
def moderator_reinstate(request, pk):
    member = get_object_or_404(
        Member,
        pk=pk,
        is_staff=False,
    )

    member.is_suspended = False
    member.is_active = True
    member.suspension_reason = ""
    member.suspended_at = None

    member.save(
        update_fields=[
            "is_suspended",
            "is_active",
            "suspension_reason",
            "suspended_at",
        ]
    )

    Suspension.objects.filter(
        member=member,
        kind="account",
        active=True,
    ).update(
        active=False,
        lifted_by=request.user,
        lifted_at=timezone.now(),
    )

    ModerationAction.objects.create(
        member=member,
        admin=request.user,
        action="reinstated",
        reason=request.POST.get(
            "reason",
            "Administrator reinstated account.",
        )[:1000],
    )

    notify(
        member,
        "Account reinstated",
        "An administrator has restored your account access.",
        "success",
    )

    record_event(
        request,
        "account_reinstated",
        f"member_id={member.pk}",
    )

    messages.success(
        request,
        "Account reinstated.",
    )

    return redirect("moderator_users")


@chief_required
@require_POST
def moderator_delete_user(request, pk):
    member = get_object_or_404(
        Member,
        pk=pk,
        is_staff=False,
    )

    if member.pk == request.user.pk:
        messages.error(
            request,
            "You cannot delete your own administrator account here.",
        )
        return redirect("moderator_users")

    username = member.username

    member.is_active = False
    member.is_suspended = True
    member.username = f"deleted-member-{member.pk}"
    member.email = ""
    member.display_name = "Deleted member"
    member.bio = ""
    member.avatar = ""

    member.save(
        update_fields=[
            "is_active",
            "is_suspended",
            "username",
            "email",
            "display_name",
            "bio",
            "avatar",
        ]
    )

    SecurityEvent.objects.create(
        actor=request.user,
        event="member_deleted_anonymised",
        detail=f"Anonymised member {username} (id={member.pk})",
    )

    messages.success(
        request,
        f"User {username} was disabled and anonymised; audit and content records were retained.",
    )

    return redirect("moderator_users")


# ---------------------------------------------------------------------
# Resource removal, audit and secure preview
# ---------------------------------------------------------------------

@admin_required
@require_POST
def moderator_delete_note(request, pk):
    note = get_object_or_404(Note, pk=pk)

    reason = request.POST.get(
        "reason",
        "Removed by administrator.",
    )[:500]

    owner = note.owner
    title = note.title

    note.status = Note.Status.REMOVED
    note.moderation_reason = reason
    note.reviewed_by = request.user
    note.reviewed_at = timezone.now()

    note.save(
        update_fields=[
            "status",
            "moderation_reason",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )

    RecoveryRecord.objects.create(
        content_type="note",
        object_id=note.pk,
        owner=owner,
        removed_by=request.user,
        reason=reason,
        retain_until=timezone.now() + timedelta(days=30),
    )

    ModerationAction.objects.create(
        note=note,
        member=owner,
        admin=request.user,
        action="deleted",
        reason=reason,
    )

    notify(
        owner,
        "Resource removed",
        f'Your resource "{title}" was removed by an administrator. Reason: {reason}',
        "danger",
    )

    record_event(
        request,
        "admin_deleted_note",
        f"note_id={note.pk}; owner_id={owner.pk}",
    )

    messages.success(
        request,
        "Resource removed and retained privately for recovery.",
    )

    return redirect("moderator_files")


@admin_required
def moderator_logs(request):
    events = SecurityEvent.objects.select_related(
        "actor"
    ).order_by("-created_at")[:200]

    actions = ModerationAction.objects.select_related(
        "admin",
        "member",
        "note",
    ).order_by("-created_at")[:100]

    return render(
        request,
        "moderator/logs.html",
        {
            "events": events,
            "actions": actions,
        },
    )


def _extract_office_preview(path, suffix):
    """Extract plain text from OOXML/ODF archives without executing document content."""
    texts = []

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()

            if suffix == ".docx":
                targets = [
                    name
                    for name in names
                    if name == "word/document.xml"
                ]

            elif suffix == ".pptx":
                targets = sorted(
                    name
                    for name in names
                    if name.startswith("ppt/slides/slide")
                    and name.endswith(".xml")
                )

            elif suffix == ".xlsx":
                targets = [
                    name
                    for name in names
                    if name.startswith("xl/sharedStrings.xml")
                    or (
                        name.startswith("xl/worksheets/sheet")
                        and name.endswith(".xml")
                    )
                ]

            elif suffix in {".odt", ".ods", ".odp"}:
                targets = (
                    ["content.xml"]
                    if "content.xml" in names
                    else []
                )

            else:
                return []

            for name in targets[:80]:
                root = ET.fromstring(archive.read(name))

                values = [
                    value.strip()
                    for value in root.itertext()
                    if value and value.strip()
                ]

                if values:
                    texts.extend(values)

    except (
        OSError,
        zipfile.BadZipFile,
        ET.ParseError,
        KeyError,
    ):
        return []

    return texts[:2500]


@admin_required
def moderator_file_preview(request, pk):
    note = get_object_or_404(Note, pk=pk)

    if (
        note.status not in ["pending", "rejected", "approved"]
        or not note.file
    ):
        raise Http404

    try:
        path = Path(note.file.path)
    except (ValueError, OSError):
        raise Http404

    if not path.exists():
        raise Http404

    suffix = path.suffix.lower()

    context = {
        "note": note,
        "preview_kind": "unsupported",
        "preview_text": [],
    }

    unsafe_scan = note.scan_status in {
        "infected",
        "error",
    }

    if unsafe_scan:
        context["preview_message"] = (
            "Preview is blocked because the security scanner marked this file "
            "as infected or returned an error. Resolve the scan issue before opening it."
        )

    elif suffix == ".pdf":
        context["preview_kind"] = "pdf"

    elif suffix in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
    }:
        context["preview_kind"] = "image"

    elif suffix in {".txt", ".md", ".rtf"}:
        try:
            context.update(
                preview_kind="text",
                preview_text=path.read_text(
                    encoding="utf-8-sig",
                    errors="replace",
                )[:50000].splitlines(),
            )
        except OSError:
            context["preview_kind"] = "unsupported"

    elif suffix == ".csv":
        try:
            with path.open(
                "r",
                encoding="utf-8-sig",
                errors="replace",
                newline="",
            ) as file_handle:
                context.update(
                    preview_kind="table",
                    preview_rows=list(
                        csv.reader(file_handle)
                    )[:101],
                )
        except OSError:
            context["preview_kind"] = "unsupported"

    elif not unsafe_scan and suffix in {
        ".docx",
        ".pptx",
        ".xlsx",
        ".odt",
        ".ods",
        ".odp",
    }:
        extracted = _extract_office_preview(
            path,
            suffix,
        )

        context.update(
            preview_kind="text" if extracted else "unsupported",
            preview_text=extracted,
        )

    context["content_url"] = reverse(
        "moderator_file_content",
        kwargs={"pk": note.pk},
    )

    return render(
        request,
        "moderator/preview.html",
        context,
    )


@admin_required
def moderator_file_content(request, pk):
    note = get_object_or_404(Note, pk=pk)

    if (
        note.status not in ["pending", "rejected", "approved"]
        or not note.file
    ):
        raise Http404

    try:
        path = Path(note.file.path)
    except (ValueError, OSError):
        raise Http404

    if (
        not path.exists()
        or path.suffix.lower() not in {
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
        }
        or note.scan_status in {"infected", "error"}
    ):
        raise Http404

    content_type = (
        mimetypes.guess_type(
            note.original_filename or path.name
        )[0]
        or "application/octet-stream"
    )

    response = FileResponse(
        note.file.open("rb"),
        content_type=content_type,
    )

    safe_name = (
        Path(note.original_filename or path.name)
        .name
        .replace('"', "")
        .replace("\r", "")
        .replace("\n", "")
    )

    response["Content-Disposition"] = (
        f'inline; filename="{safe_name}"'
    )
    response["X-Frame-Options"] = "SAMEORIGIN"
    response["Content-Security-Policy"] = (
        "default-src 'none'; frame-ancestors 'self'"
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"

    return response


# ---------------------------------------------------------------------
# Feedback configuration and reason helpers
# ---------------------------------------------------------------------

FEEDBACK_CATEGORIES = [
    ("experience", "Overall experience"),
    ("design", "Design and usability"),
    ("search", "Search and discovery"),
    ("resources", "Resource quality"),
    ("accessibility", "Accessibility"),
    ("other", "Other"),
]

WARNING_REASONS = [
    ("community_guidelines", "Community guidelines violation"),
    ("inappropriate_content", "Inappropriate or offensive content"),
    ("copyright", "Copyright or ownership concern"),
    ("spam", "Spam, misleading or duplicate submissions"),
    ("unsafe_file", "Unsafe or suspicious file submission"),
    ("harassment", "Harassment or disrespectful behaviour"),
    ("repeated_misuse", "Repeated misuse of StudyShelf"),
    ("other", "Other reason"),
]

FEEDBACK_DELETE_REASONS = [
    ("personal_information", "Contains personal or sensitive information"),
    ("abusive", "Abusive, hateful or harassing content"),
    ("spam", "Spam, advertising or repeated content"),
    ("off_topic", "Not relevant to StudyShelf"),
    ("impersonation", "Impersonation or misleading claims"),
    ("copyright", "Contains material posted without permission"),
    ("other", "Other reason"),
]


def resolve_reason(post_data, choices):
    selected = post_data.get(
        "reason_choice",
        "",
    ).strip()

    if selected == "other":
        return post_data.get(
            "custom_reason",
            "",
        ).strip()

    labels = dict(choices)

    return labels.get(selected, "")


# ---------------------------------------------------------------------
# Member feedback
# ---------------------------------------------------------------------

@login_required
def feedback_list(request):
    entries = (
        Feedback.objects.filter(
            is_deleted=False,
            visibility="published",
        )
        .select_related("member")
        .prefetch_related(
            "replies__author",
            "replies__staff_author",
            "replies__children__author",
            "replies__children__staff_author",
        )[:100]
    )

    mine = Feedback.objects.filter(
        member=request.user,
        is_deleted=False,
    )

    return render(
        request,
        "library/feedback.html",
        {
            "feedback_entries": entries,
            "my_feedback": mine,
            "categories": FEEDBACK_CATEGORIES,
        },
    )


@login_required
@rate_limit(6, 3600, "feedback-create")
def feedback_create(request):
    if request.method == "POST":
        title = request.POST.get(
            "title",
            "",
        ).strip()[:120]

        message = request.POST.get(
            "message",
            "",
        ).strip()[:2000]

        rating = request.POST.get("rating", "5")

        if (
            not title
            or len(message) < 10
            or rating not in ["1", "2", "3", "4", "5"]
        ):
            messages.error(
                request,
                "Please provide a title, at least 10 characters of feedback, and a valid rating.",
            )
        else:
            Feedback.objects.create(
                member=request.user,
                title=title,
                message=message,
                rating=int(rating),
                allow_replies=request.POST.get("allow_replies") != "off",
            )

            messages.success(
                request,
                "Thanks for sharing your feedback with the StudyShelf team.",
            )

            return redirect("feedback_list")

    return render(
        request,
        "library/feedback_form.html",
        {"entry": None},
    )


@login_required
def feedback_edit(request, pk):
    entry = get_object_or_404(
        Feedback,
        pk=pk,
        member=request.user,
        is_deleted=False,
    )

    if request.method == "POST":
        title = request.POST.get(
            "title",
            "",
        ).strip()[:120]

        message = request.POST.get(
            "message",
            "",
        ).strip()[:2000]

        rating = request.POST.get("rating", "5")

        if (
            not title
            or len(message) < 10
            or rating not in ["1", "2", "3", "4", "5"]
        ):
            messages.error(
                request,
                "Please provide a title, at least 10 characters of feedback, and a valid rating.",
            )
        else:
            entry.title = title
            entry.message = message
            entry.rating = int(rating)

            entry.save(
                update_fields=[
                    "title",
                    "message",
                    "rating",
                    "updated_at",
                ]
            )

            messages.success(
                request,
                "Your feedback was updated.",
            )

            return redirect("feedback_list")

    return render(
        request,
        "library/feedback_form.html",
        {"entry": entry},
    )


@login_required
@require_POST
def feedback_delete(request, pk):
    entry = get_object_or_404(
        Feedback,
        pk=pk,
        member=request.user,
        is_deleted=False,
    )

    entry.is_deleted = True
    entry.visibility = "removed"
    entry.deletion_reason = "Deleted by the author"
    entry.deleted_at = timezone.now()

    entry.save(
        update_fields=[
            "is_deleted",
            "visibility",
            "deletion_reason",
            "deleted_at",
        ]
    )

    messages.success(
        request,
        "Your feedback was deleted.",
    )

    return redirect("feedback_list")


# ---------------------------------------------------------------------
# Moderator feedback and discussion management
# ---------------------------------------------------------------------

@admin_required
def moderator_feedback(request):
    entries = (
        Feedback.objects.select_related("member")
        .prefetch_related(
            "replies__author",
            "replies__staff_author",
            "replies__children__author",
            "replies__children__staff_author",
        )
        .all()
    )

    state = request.GET.get("state", "visible")

    if state == "deleted":
        entries = entries.filter(is_deleted=True)

    elif state == "all":
        pass

    else:
        entries = entries.filter(is_deleted=False)
        state = "visible"

    query = request.GET.get(
        "q",
        "",
    ).strip()[:80]

    if query:
        entries = entries.filter(
            Q(member__username__icontains=query)
            | Q(member__email__icontains=query)
            | Q(title__icontains=query)
            | Q(message__icontains=query)
            | Q(replies__body__icontains=query)
            | Q(replies__author__username__icontains=query)
            | Q(replies__staff_author__username__icontains=query)
        ).distinct()

    page = Paginator(
        entries.order_by("-created_at"),
        20,
    ).get_page(request.GET.get("page"))

    return render(
        request,
        "moderator/feedback.html",
        {
            "entries": page.object_list,
            "page_obj": page,
            "state": state,
            "query": query,
            "delete_reasons": FEEDBACK_DELETE_REASONS,
        },
    )


@admin_required
@require_POST
def moderator_delete_feedback(request, pk):
    entry = get_object_or_404(
        Feedback.objects.select_related("member"),
        pk=pk,
        is_deleted=False,
    )

    reason = resolve_reason(
        request.POST,
        FEEDBACK_DELETE_REASONS,
    )[:500]

    if not reason:
        messages.error(
            request,
            "Choose a removal reason or provide a custom reason.",
        )
        return redirect("moderator_feedback")

    entry.is_deleted = True
    entry.visibility = "removed"
    entry.deleted_by = request.user
    entry.deletion_reason = reason
    entry.deleted_at = timezone.now()

    entry.save(
        update_fields=[
            "is_deleted",
            "visibility",
            "deleted_by",
            "deletion_reason",
            "deleted_at",
        ]
    )

    RecoveryRecord.objects.create(
        content_type="feedback",
        object_id=entry.pk,
        owner=entry.member,
        removed_by=request.user,
        reason=reason,
        retain_until=timezone.now() + timedelta(days=30),
    )

    notify(
        entry.member,
        "Feedback removed",
        f'Your feedback "{entry.title}" was removed by a moderator. Reason: {reason}',
        "warning",
        reverse("feedback_list"),
    )

    ModerationAction.objects.create(
        member=entry.member,
        admin=request.user,
        action="feedback_removed",
        reason=f"Feedback #{entry.pk} removed: {reason}",
    )

    record_event(
        request,
        "feedback_removed",
        f"feedback_id={entry.pk}; member_id={entry.member_id}; reason={reason}",
    )

    messages.success(
        request,
        "Feedback removed. The author has been notified with the reason.",
    )

    return redirect("moderator_feedback")


@admin_required
@require_POST
def moderator_moderate_feedback_reply(request, pk):
    """
    Independently hide, remove or restore one feedback reply.

    Hiding is reversible directly from the discussion.
    Removing creates a RecoveryRecord so the existing recovery centre
    can restore the reply.
    """
    action = request.POST.get("action", "").strip()

    reason = request.POST.get(
        "reason",
        "",
    ).strip()[:500]

    with transaction.atomic():
        reply = get_object_or_404(
            FeedbackReply.objects.select_for_update().select_related(
                "feedback",
                "author",
                "staff_author",
            ),
            pk=pk,
        )

        if action == "hide":
            if reply.status != "published":
                messages.error(
                    request,
                    "Only published replies can be hidden.",
                )
                return redirect("moderator_feedback")

            if len(reason) < 5:
                messages.error(
                    request,
                    "Provide a reason of at least 5 characters.",
                )
                return redirect("moderator_feedback")

            reply.status = "hidden"
            reply.removal_reason = reason

            reply.save(
                update_fields=[
                    "status",
                    "removal_reason",
                    "updated_at",
                ]
            )

            event_name = "feedback_reply_hidden"

        elif action == "remove":
            if reply.status == "removed":
                messages.info(
                    request,
                    "This reply has already been removed.",
                )
                return redirect("moderator_feedback")

            if len(reason) < 5:
                messages.error(
                    request,
                    "Provide a reason of at least 5 characters.",
                )
                return redirect("moderator_feedback")

            reply.status = "removed"
            reply.removal_reason = reason

            reply.save(
                update_fields=[
                    "status",
                    "removal_reason",
                    "updated_at",
                ]
            )

            RecoveryRecord.objects.create(
                content_type="reply",
                object_id=reply.pk,
                owner=reply.author,
                removed_by=request.user,
                reason=reason,
                retain_until=timezone.now() + timedelta(days=30),
            )

            event_name = "feedback_reply_removed"

        elif action == "restore":
            if reply.status != "hidden":
                messages.error(
                    request,
                    "Only hidden replies can be restored directly. Removed replies must be restored through the Recovery Centre.",
                )
                return redirect("moderator_feedback")

            reply.status = "published"
            reply.removal_reason = ""

            reply.save(
                update_fields=[
                    "status",
                    "removal_reason",
                    "updated_at",
                ]
            )

            event_name = "feedback_reply_restored"

        else:
            messages.error(
                request,
                "Invalid reply moderation action.",
            )
            return redirect("moderator_feedback")

        if reply.author_id:
            if action == "hide":
                notify(
                    reply.author,
                    "Feedback reply hidden",
                    f"Your reply was hidden by a moderator. Reason: {reason}",
                    "warning",
                    reverse("feedback_list"),
                )

            elif action == "remove":
                notify(
                    reply.author,
                    "Feedback reply removed",
                    f"Your reply was removed by a moderator. Reason: {reason}",
                    "warning",
                    reverse("feedback_list"),
                )

            elif action == "restore":
                notify(
                    reply.author,
                    "Feedback reply restored",
                    "A moderator restored your feedback reply.",
                    "success",
                    reverse("feedback_list"),
                )

        record_event(
            request,
            event_name,
            f"reply_id={reply.pk}; feedback_id={reply.feedback_id}; action={action}; reason={reason}",
        )

    messages.success(
        request,
        {
            "hide": "Reply hidden from the public discussion.",
            "remove": "Reply removed and retained in the Recovery Centre.",
            "restore": "Reply restored to the public discussion.",
        }[action],
    )

    return redirect("moderator_feedback")


# ---------------------------------------------------------------------
# Private security concerns
# ---------------------------------------------------------------------

@login_required
@rate_limit(5, 3600, "security-concern")
def security_concerns(request):
    concerns = SecurityConcern.objects.filter(
        member=request.user
    ).select_related("related_warning")

    own_warnings = WarningRecord.objects.filter(
        member=request.user,
        is_revoked=False,
    ).order_by("-created_at")

    if request.method == "POST":
        subject = request.POST.get(
            "subject",
            "",
        ).strip()[:140]

        details = request.POST.get(
            "details",
            "",
        ).strip()[:3000]

        category = request.POST.get(
            "category",
            "security_warning",
        )

        allowed = {
            "security_warning",
            "account_security",
            "suspicious_activity",
            "privacy",
            "other",
        }

        if (
            not subject
            or len(details) < 15
            or category not in allowed
        ):
            messages.error(
                request,
                "Please add a subject and explain the concern in at least 15 characters.",
            )

        else:
            warning = None

            warning_id = request.POST.get(
                "warning_id",
                "",
            ).strip()

            if category == "security_warning" and warning_id.isdigit():
                warning = WarningRecord.objects.filter(
                    pk=int(warning_id),
                    member=request.user,
                ).first()

                if not warning:
                    messages.error(
                        request,
                        "The selected warning could not be found.",
                    )

                    return render(
                        request,
                        "library/security_concerns.html",
                        {
                            "concerns": concerns,
                            "own_warnings": own_warnings,
                        },
                    )

            concern = SecurityConcern.objects.create(
                member=request.user,
                subject=subject,
                details=details,
                category=category,
                related_warning=warning,
            )

            SupportMessage.objects.create(
                concern=concern,
                author=request.user,
                body=details,
                is_internal=False,
            )

            for staff in Member.objects.filter(
                is_staff=True,
                is_active=True,
            ):
                notify(
                    staff,
                    "New security concern",
                    f"{request.user.username} submitted: {subject}",
                    "warning",
                    reverse("moderator_concerns"),
                )

            record_event(
                request,
                "security_concern_submitted",
                f"concern_id={concern.pk}",
            )

            messages.success(
                request,
                "Your concern has been sent privately to the moderation team.",
            )

            return redirect("security_concerns")

    return render(
        request,
        "library/security_concerns.html",
        {
            "concerns": concerns,
            "own_warnings": own_warnings,
        },
    )


@admin_required
def moderator_concerns(request):
    status = request.GET.get("status", "open")

    concerns = SecurityConcern.objects.select_related(
        "member",
        "assigned_to",
        "related_warning",
    )

    if status in dict(SecurityConcern.Status.choices):
        concerns = concerns.filter(status=status)

    elif status != "all":
        status = "open"
        concerns = concerns.filter(
            status__in=["open", "reviewing"]
        )

    page = Paginator(
        concerns,
        20,
    ).get_page(request.GET.get("page"))

    return render(
        request,
        "moderator/concerns.html",
        {
            "concerns": page.object_list,
            "page_obj": page,
            "status": status,
            "statuses": SecurityConcern.Status.choices,
        },
    )


@admin_required
@require_POST
def moderator_update_concern(request, pk):
    concern = get_object_or_404(
        SecurityConcern,
        pk=pk,
    )

    status = request.POST.get(
        "status",
        "reviewing",
    )

    if status not in dict(SecurityConcern.Status.choices):
        messages.error(
            request,
            "Invalid concern status.",
        )
        return redirect("moderator_concerns")

    note = request.POST.get(
        "moderator_note",
        "",
    ).strip()[:1000]

    concern.status = status
    concern.moderator_note = note
    concern.assigned_to = request.user

    concern.save(
        update_fields=[
            "status",
            "moderator_note",
            "assigned_to",
            "updated_at",
        ]
    )

    notify(
        concern.member,
        "Security concern updated",
        f'Your concern "{concern.subject}" is now {concern.get_status_display().lower()}. '
        + (f"Moderator note: {note}" if note else ""),
        "info",
        reverse("security_concerns"),
    )

    record_event(
        request,
        "security_concern_updated",
        f"concern_id={concern.pk}; status={status}",
    )

    messages.success(
        request,
        "Concern status updated and the member notified.",
    )

    return redirect("moderator_concerns")


@login_required
def security_concern_detail(request, pk):
    concern = get_object_or_404(
        SecurityConcern.objects.select_related(
            "member",
            "assigned_to",
            "related_warning",
        ).prefetch_related("messages__author"),
        pk=pk,
    )

    staff = (
        request.user.is_staff
        and request.user.role in {
            Member.Role.MODERATOR,
            Member.Role.CHIEF,
        }
    )

    if not staff and concern.member_id != request.user.id:
        raise Http404

    messages_qs = (
        concern.messages.filter(is_internal=False)
        if not staff
        else concern.messages.all()
    )

    return render(
        request,
        "library/security_concern_detail.html",
        {
            "concern": concern,
            "ticket_messages": messages_qs,
            "has_ticket_messages": messages_qs.exists(),
        },
    )


@login_required
@require_POST
@rate_limit(20, 60, "support-message")
def security_concern_reply(request, pk):
    concern = get_object_or_404(
        SecurityConcern.objects.select_related("member"),
        pk=pk,
    )

    staff = (
        request.user.is_staff
        and request.user.role in {
            Member.Role.MODERATOR,
            Member.Role.CHIEF,
        }
    )

    if not staff and concern.member_id != request.user.id:
        raise Http404

    body = request.POST.get(
        "body",
        "",
    ).strip()[:3000]

    if not body:
        messages.error(
            request,
            "Write a message before sending.",
        )
        return redirect(
            "security_concern_detail",
            pk=pk,
        )

    if concern.status == SecurityConcern.Status.CLOSED:
        messages.error(
            request,
            "This ticket is closed. Request reopening before replying.",
        )
        return redirect(
            "security_concern_detail",
            pk=pk,
        )

    internal = (
        staff
        and request.POST.get("internal_note") == "yes"
    )

    SupportMessage.objects.create(
        concern=concern,
        author=request.user,
        body=body,
        is_internal=internal,
    )

    if not internal:
        if staff:
            concern.status = SecurityConcern.Status.AWAITING

            notify(
                concern.member,
                "Support team replied",
                f'There is a new reply on your ticket "{concern.subject}".',
                "info",
                reverse(
                    "security_concern_detail",
                    kwargs={"pk": concern.pk},
                ),
            )

        else:
            concern.status = SecurityConcern.Status.REVIEWING

            if concern.assigned_to_id:
                notify(
                    concern.assigned_to,
                    "Member replied to ticket",
                    f'{concern.member.username} replied to "{concern.subject}".',
                    "info",
                    reverse(
                        "security_concern_detail",
                        kwargs={"pk": concern.pk},
                    ),
                )

            else:
                for staff_member in Member.objects.filter(
                    is_staff=True,
                    is_active=True,
                    role__in=[
                        Member.Role.MODERATOR,
                        Member.Role.CHIEF,
                    ],
                ):
                    notify(
                        staff_member,
                        "Member replied to ticket",
                        f'{concern.member.username} replied to "{concern.subject}".',
                        "info",
                        reverse("moderator_concerns"),
                    )

        concern.save(
            update_fields=[
                "status",
                "updated_at",
            ]
        )

    record_event(
        request,
        "support_ticket_message",
        f"ticket_id={concern.pk}; internal={internal}",
    )

    messages.success(
        request,
        "Your message was added to the ticket."
        if not internal
        else "Internal note saved.",
    )

    return redirect(
        "security_concern_detail",
        pk=pk,
    )


@login_required
@require_POST
def security_concern_reopen(request, pk):
    concern = get_object_or_404(
        SecurityConcern,
        pk=pk,
        member=request.user,
    )

    if concern.status not in [
        SecurityConcern.Status.CLOSED,
        SecurityConcern.Status.RESOLVED,
    ]:
        messages.info(
            request,
            "This ticket is already active.",
        )

    else:
        concern.status = SecurityConcern.Status.OPEN
        concern.save(update_fields=["status", "updated_at"])

        notify_staff = Member.objects.filter(
            is_staff=True,
            is_active=True,
            role__in=[
                Member.Role.MODERATOR,
                Member.Role.CHIEF,
            ],
        )

        for staff_member in notify_staff:
            notify(
                staff_member,
                "Ticket reopened",
                f'{request.user.username} requested to reopen "{concern.subject}".',
                "warning",
                reverse("moderator_concerns"),
            )

        messages.success(
            request,
            "Your ticket has been reopened for review.",
        )

    return redirect(
        "security_concern_detail",
        pk=pk,
    )


# ---------------------------------------------------------------------
# Feedback replies
# ---------------------------------------------------------------------

@login_required
@require_POST
@rate_limit(20, 60, "feedback-reply")
def feedback_reply_create(request, pk):
    feedback = get_object_or_404(
        Feedback,
        pk=pk,
        is_deleted=False,
        visibility="published",
    )

    is_moderator = (
        request.user.is_staff
        and request.user.role in {
            Member.Role.MODERATOR,
            Member.Role.CHIEF,
        }
    )

    # Official staff replies are permitted even when member replies
    # have been disabled or locked by the feedback author.
    if (
        not is_moderator
        and (
            not feedback.allow_replies
            or feedback.replies_locked
        )
    ):
        messages.error(
            request,
            "Replies are disabled for this feedback.",
        )
        return redirect("feedback_list")

    body = request.POST.get(
        "body",
        "",
    ).strip()[:1500]

    parent = None

    parent_id = request.POST.get(
        "parent_id",
        "",
    ).strip()

    if parent_id.isdigit():
        parent = get_object_or_404(
            FeedbackReply,
            pk=int(parent_id),
            feedback=feedback,
            status="published",
        )

        depth = 0
        cursor = parent

        while cursor.parent_id:
            depth += 1
            cursor = cursor.parent

        if depth >= 2:
            messages.error(
                request,
                "This discussion has reached its maximum reply depth. Reply to an earlier comment instead.",
            )
            return redirect("feedback_list")

    if not body:
        messages.error(
            request,
            "Please write a reply.",
        )
        return redirect("feedback_list")

    if request.user.is_staff and not is_moderator:
        messages.error(
            request,
            "This staff account is not authorised to post official feedback replies.",
        )
        return redirect("feedback_list")

    if is_moderator:
        FeedbackReply.objects.create(
            feedback=feedback,
            author=None,
            staff_author=request.user,
            is_official=True,
            parent=parent,
            body=body,
        )

        record_event(
            request,
            "official_feedback_reply",
            f"feedback_id={feedback.pk}; parent_id={parent.pk if parent else ''}",
        )

    else:
        FeedbackReply.objects.create(
            feedback=feedback,
            author=request.user,
            parent=parent,
            body=body,
        )

    messages.success(
        request,
        "Your reply has been posted.",
    )

    return redirect("feedback_list")


@login_required
@require_POST
def feedback_reply_edit(request, pk):
    reply = get_object_or_404(
        FeedbackReply,
        pk=pk,
        author=request.user,
        status="published",
    )

    body = request.POST.get(
        "body",
        "",
    ).strip()[:1500]

    if body:
        reply.body = body
        reply.save(
            update_fields=[
                "body",
                "updated_at",
            ]
        )

    return redirect("feedback_list")


@login_required
@require_POST
def feedback_reply_delete(request, pk):
    reply = get_object_or_404(
        FeedbackReply,
        pk=pk,
        author=request.user,
        status="published",
    )

    reply.status = "removed"
    reply.removal_reason = "Withdrawn by author"

    reply.save(
        update_fields=[
            "status",
            "removal_reason",
            "updated_at",
        ]
    )

    RecoveryRecord.objects.create(
        content_type="reply",
        object_id=reply.pk,
        owner=request.user,
        reason="Withdrawn by author",
        retain_until=timezone.now() + timedelta(days=30),
    )

    return redirect("feedback_list")


@login_required
@require_POST
def feedback_reply_report(request, pk):
    reply = get_object_or_404(
        FeedbackReply,
        pk=pk,
        status="published",
    )

    if reply.author_id == request.user.id:
        messages.error(
            request,
            "You cannot report your own reply.",
        )

    else:
        ContentReport.objects.create(
            reporter=request.user,
            reply=reply,
            reason=request.POST.get(
                "reason",
                "Inappropriate reply",
            )[:500],
            details=request.POST.get(
                "details",
                "",
            )[:2000],
        )

        messages.success(
            request,
            "Reply reported for review.",
        )

    return redirect("feedback_list")


@login_required
@require_POST
def feedback_reply_setting(request, pk):
    entry = get_object_or_404(
        Feedback,
        pk=pk,
        member=request.user,
        is_deleted=False,
    )

    entry.allow_replies = (
        request.POST.get("allow_replies") == "yes"
    )

    entry.save(
        update_fields=["allow_replies"]
    )

    messages.success(
        request,
        "Reply preference updated.",
    )

    return redirect("feedback_list")


# ---------------------------------------------------------------------
# Warning appeals
# ---------------------------------------------------------------------

@login_required
@require_POST
def challenge_warning(request, pk):
    warning = get_object_or_404(
        WarningRecord,
        pk=pk,
        member=request.user,
        is_revoked=False,
    )

    explanation = request.POST.get(
        "explanation",
        "",
    ).strip()[:2000]

    if len(explanation) < 10:
        messages.error(
            request,
            "Please explain your concern in at least 10 characters.",
        )

    elif WarningChallenge.objects.filter(
        warning=warning,
        status="pending",
    ).exists():
        messages.info(
            request,
            "This warning already has a pending review.",
        )

    else:
        WarningChallenge.objects.create(
            warning=warning,
            member=request.user,
            explanation=explanation,
        )

        warning.review_status = "pending"
        warning.save(
            update_fields=["review_status"]
        )

        for staff in Member.objects.filter(
            is_staff=True,
            is_active=True,
        ):
            notify(
                staff,
                "Warning challenged",
                f"{request.user.username} challenged warning #{warning.pk}.",
                "warning",
                reverse("moderator_challenges"),
            )

        messages.success(
            request,
            "Your warning challenge has been submitted.",
        )

    return redirect("security_concerns")


@admin_required
def moderator_challenges(request):
    qs = WarningChallenge.objects.select_related(
        "warning",
        "member",
        "warning__issued_by",
    ).order_by("created_at")

    return render(
        request,
        "moderator/challenges.html",
        {"challenges": qs[:100]},
    )


@admin_required
@require_POST
def moderator_decide_challenge(request, pk):
    challenge = get_object_or_404(
        WarningChallenge.objects.select_related(
            "warning",
            "member",
        ),
        pk=pk,
        status="pending",
    )

    if (
        challenge.warning.issued_by_id == request.user.id
        and getattr(request.user, "role", "user") != "chief_moderator"
    ):
        messages.error(
            request,
            "Another moderator or the Chief must review your own warning decision.",
        )
        return redirect("moderator_challenges")

    decision = request.POST.get("decision")
    reason = request.POST.get(
        "reason",
        "",
    ).strip()[:1000]

    if decision not in {"upheld", "revoked"} or len(reason) < 5:
        messages.error(
            request,
            "Select a decision and provide a reason.",
        )
        return redirect("moderator_challenges")

    challenge.status = decision
    challenge.reviewer = request.user
    challenge.decision_reason = reason
    challenge.decided_at = timezone.now()
    challenge.save()

    warning = challenge.warning
    warning.review_status = decision

    if decision == "revoked":
        warning.is_revoked = True
        warning.revoked_by = request.user
        warning.revoked_at = timezone.now()

        ModerationAction.objects.create(
            member=challenge.member,
            admin=request.user,
            action="warning_revoked",
            reason=f"Warning challenge #{challenge.pk}: {reason}",
        )

        notify(
            challenge.member,
            "Warning revoked",
            "Your warning challenge was successful. The warning no longer counts toward thresholds.",
            "success",
        )

        active_count = (
            WarningRecord.objects.filter(
                member=challenge.member,
                is_revoked=False,
            )
            .filter(
                Q(expires_at__isnull=True)
                | Q(expires_at__gt=timezone.now())
            )
            .count()
        )

        if active_count < 2:
            Suspension.objects.filter(
                member=challenge.member,
                kind="upload",
                active=True,
                reason__startswith="Automatic 7-day",
            ).update(
                active=False,
                lifted_by=request.user,
                lifted_at=timezone.now(),
            )

        if (
            active_count < 5
            and challenge.member.is_suspended
            and challenge.member.suspension_reason.startswith(
                "Account suspended after reaching five"
            )
        ):
            challenge.member.is_suspended = False
            challenge.member.is_active = True
            challenge.member.suspension_reason = ""
            challenge.member.suspended_at = None

            challenge.member.save(
                update_fields=[
                    "is_suspended",
                    "is_active",
                    "suspension_reason",
                    "suspended_at",
                ]
            )

            Suspension.objects.filter(
                member=challenge.member,
                kind="account",
                active=True,
            ).update(
                active=False,
                lifted_by=request.user,
                lifted_at=timezone.now(),
            )

    else:
        notify(
            challenge.member,
            "Warning upheld",
            "Your warning challenge was reviewed and the warning remains active. Reason: "
            + reason,
            "warning",
        )

    warning.save()

    record_event(
        request,
        "warning_challenge_decided",
        f"challenge_id={pk}; decision={decision}; reason={reason}",
    )

    return redirect("moderator_challenges")


# ---------------------------------------------------------------------
# Recovery centre and restoration
# ---------------------------------------------------------------------

@admin_required
def moderator_recovery(request):
    records = RecoveryRecord.objects.select_related(
        "owner",
        "removed_by",
        "restored_by",
    )

    return render(
        request,
        "moderator/recovery.html",
        {"records": records[:200]},
    )


@admin_required
@require_POST
def moderator_restore_content(request, pk):
    record = get_object_or_404(
        RecoveryRecord,
        pk=pk,
        restored_at__isnull=True,
        purged_at__isnull=True,
    )

    reason = request.POST.get(
        "reason",
        "",
    ).strip()[:500]

    if len(reason) < 5:
        messages.error(
            request,
            "A restoration reason is required.",
        )
        return redirect("moderator_recovery")

    model = {
        "note": Note,
        "feedback": Feedback,
        "reply": FeedbackReply,
    }[record.content_type]

    obj = get_object_or_404(
        model,
        pk=record.object_id,
    )

    if record.content_type == "note":
        if not obj.file:
            messages.error(
                request,
                "The original file is unavailable; the resource cannot be restored.",
            )
            return redirect("moderator_recovery")

        obj.status = Note.Status.PENDING
        obj.moderation_reason = "Restored for re-review: " + reason
        obj.reviewed_by = None
        obj.reviewed_at = None
        obj.save()

    elif record.content_type == "feedback":
        obj.is_deleted = False
        obj.visibility = "published"
        obj.deleted_at = None

        obj.save(
            update_fields=[
                "is_deleted",
                "visibility",
                "deleted_at",
            ]
        )

    else:
        obj.status = "published"
        obj.removal_reason = ""

        obj.save(
            update_fields=[
                "status",
                "removal_reason",
                "updated_at",
            ]
        )

    record.restored_at = timezone.now()
    record.restored_by = request.user

    record.save(
        update_fields=[
            "restored_at",
            "restored_by",
        ]
    )

    if record.owner_id:
        notify(
            record.owner,
            "Content restored",
            "Your content has been restored. Resources return to review before public listing.",
            "success",
        )

    record_event(
        request,
        "content_restored",
        f"record_id={pk}; reason={reason}",
    )

    messages.success(
        request,
        "Content restored successfully.",
    )

    return redirect("moderator_recovery")


@chief_required
@require_POST
def chief_purge_content(request, pk):
    record = get_object_or_404(
        RecoveryRecord,
        pk=pk,
        restored_at__isnull=True,
        purged_at__isnull=True,
        retain_until__lte=timezone.now(),
    )

    # Check reports against the exact content object being purged.
    # A pending warning appeal elsewhere must not block unrelated purges.
    active_reports = ContentReport.objects.filter(
        status__in=["open", "reviewing"]
    )

    if record.content_type == "note":
        active_reports = active_reports.filter(
            note_id=record.object_id
        )

    elif record.content_type == "feedback":
        active_reports = active_reports.filter(
            feedback_id=record.object_id
        )

    elif record.content_type == "reply":
        active_reports = active_reports.filter(
            reply_id=record.object_id
        )

    if active_reports.exists():
        messages.error(
            request,
            "This content has an active report. Resolve the linked report before permanent deletion.",
        )
        return redirect("moderator_recovery")

    reason = request.POST.get(
        "reason",
        "",
    ).strip()[:500]

    if len(reason) < 10:
        messages.error(
            request,
            "A detailed purge reason is required.",
        )
        return redirect("moderator_recovery")

    model = {
        "note": Note,
        "feedback": Feedback,
        "reply": FeedbackReply,
    }[record.content_type]

    obj = model.objects.filter(
        pk=record.object_id
    ).first()

    if obj:
        if record.content_type == "note" and obj.file:
            obj.file.delete(save=False)

        obj.delete()

    record.purged_at = timezone.now()
    record.purged_by = request.user
    record.reason = reason

    record.save(
        update_fields=[
            "purged_at",
            "purged_by",
            "reason",
        ]
    )

    record_event(
        request,
        "content_permanently_purged",
        f"record_id={pk}; reason={reason}",
    )

    messages.success(
        request,
        "Content permanently purged.",
    )

    return redirect("moderator_recovery")


# ---------------------------------------------------------------------
# Staff password and MFA
# ---------------------------------------------------------------------

@login_required
def staff_password_change(request):
    if (
        not request.user.is_staff
        or request.user.role not in {
            Member.Role.MODERATOR,
            Member.Role.CHIEF,
        }
        or not request.user.must_change_password
    ):
        return redirect("dashboard")

    if request.method == "POST":
        password1 = request.POST.get(
            "new_password1",
            "",
        )

        password2 = request.POST.get(
            "new_password2",
            "",
        )

        if password1 != password2:
            messages.error(
                request,
                "The passwords do not match.",
            )

        else:
            try:
                validate_password(
                    password1,
                    request.user,
                )

            except ValidationError as exc:
                for error in exc.messages:
                    messages.error(request, error)

            else:
                request.user.set_password(password1)
                request.user.must_change_password = False
                request.user.mfa_enabled = False
                request.user.mfa_secret = ""

                request.user.save(
                    update_fields=[
                        "password",
                        "must_change_password",
                        "mfa_enabled",
                        "mfa_secret",
                    ]
                )

                from django.contrib.auth import update_session_auth_hash

                update_session_auth_hash(
                    request,
                    request.user,
                )

                request.session.pop(
                    "staff_mfa_verified",
                    None,
                )

                record_event(
                    request,
                    "staff_temporary_password_changed",
                    "Forced first-login password change",
                )

                messages.success(
                    request,
                    "Password updated. Set up your authenticator to continue.",
                )

                return redirect("staff_mfa_setup")

    return render(
        request,
        "registration/staff_password_change.html",
    )


@login_required
def staff_mfa_setup(request):
    if (
        not request.user.is_staff
        or request.user.role not in {
            Member.Role.MODERATOR,
            Member.Role.CHIEF,
        }
    ):
        raise Http404

    if request.user.mfa_enabled:
        return redirect("staff_mfa_verify")

    secret = (
        decrypt_secret(request.user.mfa_secret)
        if request.user.mfa_secret
        else pyotp.random_base32()
    )

    if not request.user.mfa_secret:
        request.user.mfa_secret = encrypt_secret(secret)
        request.user.save(
            update_fields=["mfa_secret"]
        )

    if request.method == "POST":
        code = request.POST.get(
            "code",
            "",
        ).strip()

        if pyotp.TOTP(secret).verify(
            code,
            valid_window=1,
        ):
            request.user.mfa_enabled = True
            request.user.save(
                update_fields=["mfa_enabled"]
            )

            request.session["staff_mfa_verified"] = True

            record_event(
                request,
                "staff_mfa_enabled",
                "TOTP enrolled",
            )

            return redirect(
                "chief_dashboard"
                if request.user.role == Member.Role.CHIEF
                else "moderator_dashboard"
            )

        messages.error(
            request,
            "That verification code was not valid.",
        )

    uri = pyotp.TOTP(secret).provisioning_uri(
        name=request.user.username,
        issuer_name="StudyShelf Staff",
    )

    return render(
        request,
        "registration/staff_mfa_setup.html",
        {
            "secret": secret,
            "provisioning_uri": uri,
        },
    )


@login_required
def staff_mfa_verify(request):
    if (
        not request.user.is_staff
        or request.user.role not in {
            Member.Role.MODERATOR,
            Member.Role.CHIEF,
        }
        or not request.user.mfa_enabled
    ):
        return redirect("staff_mfa_setup")

    if request.method == "POST":
        try:
            secret = decrypt_secret(
                request.user.mfa_secret
            )

        except Exception:
            messages.error(
                request,
                "MFA setup needs to be reset by the Chief.",
            )
            return redirect("staff_mfa_setup")

        if pyotp.TOTP(secret).verify(
            request.POST.get("code", "").strip(),
            valid_window=1,
        ):
            request.session["staff_mfa_verified"] = True

            return redirect(
                "chief_dashboard"
                if request.user.role == Member.Role.CHIEF
                else "moderator_dashboard"
            )

        messages.error(
            request,
            "Invalid code. Try again.",
        )

    return render(
        request,
        "registration/staff_mfa_verify.html",
    )


# ---------------------------------------------------------------------
# Chief Moderator dashboard and oversight
# ---------------------------------------------------------------------

@chief_required
def chief_dashboard(request):
    active_members = Member.objects.filter(
        is_staff=False,
        is_active=True,
    )

    all_members = Member.objects.filter(
        is_staff=False,
    )

    all_notes = Note.objects.all()

    open_concerns = SecurityConcern.objects.filter(
        status__in=[
            "open",
            "reviewing",
            "awaiting_user",
        ]
    )

    open_reports = ContentReport.objects.filter(
        status__in=["open", "reviewing"]
    )

    active_warnings = WarningRecord.objects.filter(
        is_revoked=False
    )

    context = {
        "moderator_count": Member.objects.filter(
            role=Member.Role.MODERATOR,
            is_active=True,
        ).count(),

        "inactive_moderator_count": Member.objects.filter(
            role=Member.Role.MODERATOR,
            is_active=False,
        ).count(),

        "pending_count": all_notes.filter(
            status=Note.Status.PENDING
        ).count(),

        "open_concerns_count": open_concerns.count(),

        "open_reports_count": open_reports.count(),

        "recent_events": SecurityEvent.objects.select_related(
            "actor"
        ).order_by("-created_at")[:12],

        "total_members": all_members.count(),

        "active_members": active_members.count(),

        "inactive_members": all_members.filter(
            is_active=False
        ).count(),

        "total_resources": all_notes.count(),

        "approved_resources": all_notes.filter(
            status=Note.Status.APPROVED,
            visibility=Note.Visibility.PUBLIC,
        ).count(),

        "pending_resources": all_notes.filter(
            status=Note.Status.PENDING
        ).count(),

        "rejected_resources": all_notes.filter(
            status=Note.Status.REJECTED
        ).count(),

        "removed_resources": all_notes.filter(
            status=Note.Status.REMOVED
        ).count(),

        "total_downloads": sum(
            all_notes.values_list(
                "downloads",
                flat=True,
            )
        ),

        "feedback_total": Feedback.objects.filter(
            is_deleted=False
        ).count(),

        "feedback_reply_total": FeedbackReply.objects.filter(
            status="published"
        ).count(),

        "hidden_reply_total": FeedbackReply.objects.filter(
            status="hidden"
        ).count(),

        "removed_reply_total": FeedbackReply.objects.filter(
            status="removed"
        ).count(),

        "warning_total": active_warnings.count(),

        "suspended_member_total": Member.objects.filter(
            is_suspended=True
        ).count(),

        "active_ticket_total": open_concerns.count(),

        "pending_appeal_total": WarningChallenge.objects.filter(
            status="pending"
        ).count(),

        "user_growth_series": _last_seven_day_series(
            all_members,
            date_field="date_joined",
        ),

        "resource_series": _last_seven_day_series(
            all_notes
        ),

        "ticket_series": _last_seven_day_series(
            SecurityConcern.objects.all()
        ),

        "review_series": _last_seven_day_series(
            ModerationAction.objects.filter(
                action__in=["approved", "rejected"]
            )
        ),

        "recent_resources": all_notes.select_related(
            "owner"
        ).order_by("-created_at")[:8],

        "recent_review_actions": ModerationAction.objects.select_related(
            "admin",
            "member",
            "note",
        ).order_by("-created_at")[:12],

        "moderators": Member.objects.filter(
            role=Member.Role.MODERATOR
        ).order_by("username")[:10],
    }

    return render(
        request,
        "moderator/chief_dashboard.html",
        context,
    )


@chief_required
def chief_moderators(request):
    moderators = Member.objects.filter(
        role=Member.Role.MODERATOR
    ).order_by("username")

    credentials = request.session.pop(
        "new_moderator_credentials",
        None,
    )

    return render(
        request,
        "moderator/staff_management.html",
        {
            "moderators": moderators,
            "new_credentials": credentials,
        },
    )


@chief_required
@require_POST
def chief_create_moderator(request):
    username = request.POST.get(
        "username",
        "",
    ).strip()

    email = request.POST.get(
        "email",
        "",
    ).strip().lower()

    if (
        not username
        or not email
        or Member.objects.filter(
            username__iexact=username
        ).exists()
        or Member.objects.filter(
            email__iexact=email
        ).exists()
    ):
        messages.error(
            request,
            "Enter a unique username and email address.",
        )
        return redirect("chief_moderators")

    temporary_password = get_random_string(20)

    moderator = Member(
        username=username,
        email=email,
        role=Member.Role.MODERATOR,
        is_staff=True,
        is_active=True,
        must_change_password=True,
        mfa_enabled=False,
    )

    moderator.set_password(temporary_password)
    moderator.save()

    record_event(
        request,
        "moderator_account_created",
        f"moderator_id={moderator.pk}; username={username}",
    )

    request.session["new_moderator_credentials"] = {
        "username": username,
        "password": temporary_password,
    }

    messages.success(
        request,
        "Moderator created. Copy the temporary password now; it will not be shown again.",
    )

    return redirect("chief_moderators")


@chief_required
@require_POST
def chief_toggle_moderator(request, pk):
    moderator = get_object_or_404(
        Member,
        pk=pk,
        role=Member.Role.MODERATOR,
    )

    moderator.is_active = not moderator.is_active

    moderator.save(
        update_fields=["is_active"]
    )

    record_event(
        request,
        "moderator_access_changed",
        f"moderator_id={moderator.pk}; active={moderator.is_active}",
    )

    messages.success(
        request,
        f"Moderator {moderator.username} is now "
        f"{'active' if moderator.is_active else 'deactivated'}.",
    )

    return redirect("chief_moderators")


# ---------------------------------------------------------------------
# Manual account suspension
# ---------------------------------------------------------------------

MANUAL_ACCOUNT_SUSPENSION_REASONS = {
    "malicious_upload": "Repeated malicious file submissions",
    "harassment": "Severe harassment or targeted abuse",
    "impersonation": "Impersonation or account manipulation",
    "security_bypass": "Deliberate security-control bypass",
    "credible_threat": "Credible threats or severe abuse",
}


@admin_required
@require_POST
def moderator_manual_account_suspend(request, pk):
    member = get_object_or_404(
        Member,
        pk=pk,
        is_staff=False,
    )

    reason_key = request.POST.get(
        "reason_choice",
        "",
    )

    reason = MANUAL_ACCOUNT_SUSPENSION_REASONS.get(
        reason_key
    )

    explanation = request.POST.get(
        "explanation",
        "",
    ).strip()[:1000]

    days = request.POST.get(
        "days",
        "3",
    )

    if (
        not reason
        or len(explanation) < 10
        or not days.isdigit()
        or not 1 <= int(days) <= 7
    ):
        messages.error(
            request,
            "Choose a serious violation, explain the evidence, and select a 1–7 day protective suspension.",
        )
        return redirect("moderator_users")

    member.is_suspended = True
    member.is_active = False
    member.suspended_at = timezone.now()
    member.suspension_reason = f"{reason}: {explanation}"

    member.save(
        update_fields=[
            "is_suspended",
            "is_active",
            "suspended_at",
            "suspension_reason",
        ]
    )

    Suspension.objects.create(
        member=member,
        kind="account",
        reason=member.suspension_reason,
        ends_at=timezone.now() + timedelta(days=int(days)),
        created_by=request.user,
    )

    ModerationAction.objects.create(
        member=member,
        admin=request.user,
        action="suspended",
        reason=(
            f"Urgent protective suspension ({days} days): "
            f"{member.suspension_reason}"
        ),
    )

    notify(
        member,
        "Temporary account suspension",
        f"Your account was temporarily suspended for {days} days pending Chief Moderator review. Reason: {member.suspension_reason}",
        "danger",
    )

    for chief in Member.objects.filter(
        is_staff=True,
        is_active=True,
    ).filter(
        Q(role="chief_moderator")
        | Q(is_superuser=True)
    ):
        notify(
            chief,
            "Urgent suspension review",
            f"Review the protective suspension of {member.username} within 24 hours.",
            "warning",
            reverse("moderator_users"),
        )

    record_event(
        request,
        "manual_account_suspension",
        f"member_id={member.pk}; duration={days}; reason={reason_key}",
    )

    return redirect("moderator_users")


# ---------------------------------------------------------------------
# Account deletion requests
# ---------------------------------------------------------------------

@login_required
@require_POST
def request_account_deletion(request):
    from .models import AccountDeletionRequest

    if request.user.is_staff:
        messages.error(
            request,
            "Staff account deletion must follow the owner-controlled staff process.",
        )
        return redirect("profile")

    if AccountDeletionRequest.objects.filter(
        member=request.user,
        status="pending",
    ).exists():
        messages.info(
            request,
            "You already have a pending account deletion request.",
        )

    else:
        AccountDeletionRequest.objects.create(
            member=request.user,
            reason=request.POST.get(
                "reason",
                "",
            ).strip()[:1000],
        )

        for chief in Member.objects.filter(
            is_staff=True,
        ).filter(
            Q(role="chief_moderator")
            | Q(is_superuser=True)
        ):
            notify(
                chief,
                "Account deletion request",
                f"{request.user.username} requested account deletion.",
                "warning",
                reverse("moderator_deletion_requests"),
            )

        messages.success(
            request,
            "Your deletion request has been sent to the Chief Moderator.",
        )

    return redirect("profile")


@chief_required
def moderator_deletion_requests(request):
    from .models import AccountDeletionRequest

    return render(
        request,
        "moderator/deletion_requests.html",
        {
            "requests": AccountDeletionRequest.objects.select_related(
                "member"
            ).filter(status="pending"),
        },
    )


@chief_required
@require_POST
def chief_decide_deletion_request(request, pk):
    from .models import AccountDeletionRequest

    item = get_object_or_404(
        AccountDeletionRequest.objects.select_related("member"),
        pk=pk,
        status="pending",
    )

    decision = request.POST.get("decision")

    if decision == "approve":
        member = item.member

        member.is_active = False
        member.username = f"deleted-member-{member.pk}"
        member.email = ""
        member.display_name = "Deleted member"
        member.bio = ""
        member.avatar = ""

        member.save(
            update_fields=[
                "is_active",
                "username",
                "email",
                "display_name",
                "bio",
                "avatar",
            ]
        )

        item.status = "approved"

    elif decision == "reject":
        item.status = "rejected"

    else:
        return redirect("moderator_deletion_requests")

    item.reviewed_by = request.user
    item.reviewed_at = timezone.now()

    item.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
        ]
    )

    record_event(
        request,
        "account_deletion_request_decided",
        f"request_id={pk}; decision={decision}",
    )

    return redirect("moderator_deletion_requests")