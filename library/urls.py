from django.urls import path
from django.contrib.auth import views as auth_views

from . import views
from .security import rate_limit


urlpatterns = [
    # Public library
    path("", views.home, name="home"),
    path("library/", views.note_list, name="note_list"),
    path("notes/<slug:slug>/", views.note_detail, name="note_detail"),
    path("notes/<slug:slug>/download/", views.download_note, name="download_note"),
    path("notes/<slug:slug>/edit/", views.note_edit, name="note_edit"),
    path("notes/<slug:slug>/delete/", views.note_delete, name="note_delete"),
    path("notes/<slug:slug>/save/", views.toggle_bookmark, name="toggle_bookmark"),
    path("share/", views.note_create, name="note_create"),

    # Authentication and account
    path("accounts/register/", views.register, name="register"),
    path("members/<int:pk>/avatar/", views.member_avatar, name="member_avatar"),
    path(
        "accounts/login/",
        rate_limit(
            12,
            900,
            "login",
        )(
            auth_views.LoginView.as_view(
                template_name="registration/login.html",
                redirect_authenticated_user=True,
            )
        ),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("staff/password/change/", views.staff_password_change, name="staff_password_change"),
    path("staff/mfa/setup/", views.staff_mfa_setup, name="staff_mfa_setup"),
    path("staff/mfa/verify/", views.staff_mfa_verify, name="staff_mfa_verify"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("profile/", views.profile, name="profile"),
    path("account/deletion/request/", views.request_account_deletion, name="request_account_deletion"),

    # Notifications
    path("notifications/", views.notification_list, name="notifications"),
    path("notifications/<int:pk>/read/", views.notification_read, name="notification_read"),
    path("notifications/read-all/", views.notifications_read_all, name="notifications_read_all"),

    # Member feedback
    path("feedback/", views.feedback_list, name="feedback_list"),
    path("feedback/new/", views.feedback_create, name="feedback_create"),
    path("feedback/<int:pk>/edit/", views.feedback_edit, name="feedback_edit"),
    path("feedback/<int:pk>/delete/", views.feedback_delete, name="feedback_delete"),
    path("feedback/<int:pk>/reply/", views.feedback_reply_create, name="feedback_reply_create"),
    path("feedback/replies/<int:pk>/edit/", views.feedback_reply_edit, name="feedback_reply_edit"),
    path("feedback/replies/<int:pk>/delete/", views.feedback_reply_delete, name="feedback_reply_delete"),
    path("feedback/replies/<int:pk>/report/", views.feedback_reply_report, name="feedback_reply_report"),
    path("feedback/<int:pk>/reply-setting/", views.feedback_reply_setting, name="feedback_reply_setting"),
    path("warnings/<int:pk>/challenge/", views.challenge_warning, name="challenge_warning"),

    # Private security concerns
    path("security-concerns/", views.security_concerns, name="security_concerns"),
    path("security-concerns/<int:pk>/", views.security_concern_detail, name="security_concern_detail"),
    path("security-concerns/<int:pk>/reply/", views.security_concern_reply, name="security_concern_reply"),
    path("security-concerns/<int:pk>/reopen/", views.security_concern_reopen, name="security_concern_reopen"),

    # Moderator dashboards
    path("moderator/", views.moderator_dashboard, name="moderator_dashboard"),
    path("moderator/chief/", views.chief_dashboard, name="chief_dashboard"),

    # Chief Moderator staff management
    path("moderator/chief/moderators/", views.chief_moderators, name="chief_moderators"),
    path("moderator/chief/moderators/create/", views.chief_create_moderator, name="chief_create_moderator"),
    path("moderator/chief/moderators/<int:pk>/toggle/", views.chief_toggle_moderator, name="chief_toggle_moderator"),

    # Warning appeals
    path("moderator/challenges/", views.moderator_challenges, name="moderator_challenges"),
    path("moderator/challenges/<int:pk>/decide/", views.moderator_decide_challenge, name="moderator_decide_challenge"),

    # Recovery centre
    path("moderator/recovery/", views.moderator_recovery, name="moderator_recovery"),
    path("moderator/recovery/<int:pk>/restore/", views.moderator_restore_content, name="moderator_restore_content"),
    path("moderator/recovery/<int:pk>/purge/", views.chief_purge_content, name="chief_purge_content"),

    # Account deletion requests
    path("moderator/deletion-requests/", views.moderator_deletion_requests, name="moderator_deletion_requests"),
    path("moderator/deletion-requests/<int:pk>/decide/", views.chief_decide_deletion_request, name="chief_decide_deletion_request"),

    # Resource review and management
    path("moderator/queue/", views.moderator_queue, name="moderator_queue"),
    path("moderator/files/", views.moderator_files, name="moderator_files"),
    path("moderator/files/<int:pk>/approve/", views.moderator_approve, name="moderator_approve"),
    path("moderator/files/<int:pk>/reject/", views.moderator_reject, name="moderator_reject"),
    path("moderator/files/<int:pk>/delete/", views.moderator_delete_note, name="moderator_delete_note"),
    path("moderator/files/<int:pk>/preview/", views.moderator_file_preview, name="moderator_file_preview"),
    path(
        "moderator/files/<int:pk>/download/",
        views.moderator_file_preview,
        name="moderator_file_preview_legacy",
    ),
    path(
        "moderator/files/<int:pk>/preview/content/",
        views.moderator_file_content,
        name="moderator_file_content",
    ),

    # Member moderation
    path("moderator/users/", views.moderator_users, name="moderator_users"),
    path("moderator/users/<int:pk>/warn/", views.moderator_warn, name="moderator_warn"),
    path("moderator/warnings/<int:pk>/revoke/", views.moderator_revoke_warning, name="moderator_revoke_warning"),
    path("moderator/users/<int:pk>/suspend-uploads/", views.moderator_upload_suspend, name="moderator_upload_suspend"),
    path("moderator/users/<int:pk>/suspend-account/", views.moderator_manual_account_suspend, name="moderator_manual_account_suspend"),
    path("moderator/users/<int:pk>/reinstate/", views.moderator_reinstate, name="moderator_reinstate"),
    path("moderator/users/<int:pk>/delete/", views.moderator_delete_user, name="moderator_delete_user"),

    # Audit logs
    path("moderator/logs/", views.moderator_logs, name="moderator_logs"),

    # Feedback moderation
    path("moderator/feedback/", views.moderator_feedback, name="moderator_feedback"),
    path("moderator/feedback/<int:pk>/delete/", views.moderator_delete_feedback, name="moderator_delete_feedback"),

    # Individual feedback reply moderation
    path(
        "moderator/feedback/replies/<int:pk>/moderate/",
        views.moderator_moderate_feedback_reply,
        name="moderator_moderate_feedback_reply",
    ),

    # Security concern moderation
    path("moderator/concerns/", views.moderator_concerns, name="moderator_concerns"),
    path("moderator/concerns/<int:pk>/update/", views.moderator_update_concern, name="moderator_update_concern"),
]