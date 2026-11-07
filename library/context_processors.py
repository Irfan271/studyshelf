from .models import Notification
def notification_context(request):
    if request.user.is_authenticated: return {"unread_notifications":Notification.objects.filter(member=request.user,is_read=False).count()}
    return {"unread_notifications":0}
