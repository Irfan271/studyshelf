from functools import wraps
from django.core.cache import cache
from django.http import HttpResponse,HttpResponseForbidden
from django.shortcuts import redirect
from django.db.models import Q
from django.utils import timezone
from .models import SecurityEvent,Suspension
def client_ip(request): return request.META.get("REMOTE_ADDR")
def record_event(request,event,detail=""):
    SecurityEvent.objects.create(actor=request.user if request.user.is_authenticated else None,event=event,detail=detail[:500],ip_address=client_ip(request))
def rate_limit(limit=8,window=300,prefix="request",methods=("POST",)):
    def deco(view):
        @wraps(view)
        def wrapped(request,*args,**kwargs):
            if request.method not in methods:
                return view(request,*args,**kwargs)
            ip=client_ip(request) or "unknown"; key=f"studyshelf:{prefix}:{ip}"
            cache.add(key,0,timeout=window)
            try:
                count=cache.incr(key)
            except ValueError:
                cache.set(key,1,timeout=window)
                count=1
            if count>limit:
                if request.user.is_authenticated: record_event(request,"rate_limited",prefix)
                return HttpResponse("Too many requests. Please try again later.",status=429)
            return view(request,*args,**kwargs)
        return wrapped
    return deco
def admin_required(view):
    @wraps(view)
    def wrapped(request,*args,**kwargs):
        if not request.user.is_authenticated: return redirect("login")
        if not request.user.is_staff or getattr(request.user, "role", "user") not in {"moderator", "chief_moderator"}: return HttpResponseForbidden("Staff access required.")
        return view(request,*args,**kwargs)
    return wrapped
def active_upload_suspension(user):
    return Suspension.objects.filter(member=user,kind="upload",active=True,starts_at__lte=timezone.now()).filter(Q(ends_at__isnull=True)|Q(ends_at__gt=timezone.now())).exists()

def chief_required(view):
    @wraps(view)
    def wrapped(request,*args,**kwargs):
        if not request.user.is_authenticated: return redirect("login")
        if not request.user.is_staff or getattr(request.user, "role", "user") != "chief_moderator":
            return HttpResponseForbidden("Chief Moderator access required.")
        return view(request,*args,**kwargs)
    return wrapped
