from datetime import date, datetime, time, timedelta
from django.utils import timezone


def get_restaurant_context(request):
    """
    Safely get the active restaurant for the request user or fall back to default.
    Ensures strict restaurant data isolation.
    """
    from restaurant.models import Restaurant

    if hasattr(request, "user") and request.user.is_authenticated:
        if getattr(request.user, "restaurant", None):
            return request.user.restaurant
        # If user has employee profile with restaurant
        emp_profile = getattr(request.user, "employee_profile", None)
        if emp_profile and getattr(emp_profile, "restaurant", None):
            return emp_profile.restaurant

    # Fallback to restaurant from session if any, or first restaurant
    rest_id = request.session.get("active_restaurant_id") if hasattr(request, "session") else None
    if rest_id:
        rest = Restaurant.objects.filter(pk=rest_id).first()
        if rest:
            return rest

    return Restaurant.objects.first()


def parse_date_range(request, default_preset="today"):
    """
    Parse date range parameters from request.GET.
    Supports: today, yesterday, this_week, this_month, last_month, custom.
    Uses local business-day boundaries (start of day 00:00:00 to end of day 23:59:59.999999).
    """
    tz = timezone.get_current_timezone()
    now_local = timezone.localtime(timezone.now(), tz)
    today = now_local.date()

    preset = request.GET.get("preset", "").strip().lower()
    start_str = request.GET.get("start_date", "").strip()
    end_str = request.GET.get("end_date", "").strip()

    if not preset:
        if start_str or end_str:
            preset = "custom"
        else:
            preset = default_preset

    if preset == "yesterday":
        start_date = today - timedelta(days=1)
        end_date = start_date
        label = "Yesterday"
    elif preset == "this_week":
        start_date = today - timedelta(days=today.weekday())
        end_date = today
        label = "This Week"
    elif preset == "this_month":
        start_date = today.replace(day=1)
        end_date = today
        label = "This Month"
    elif preset == "last_month":
        first_of_this_month = today.replace(day=1)
        last_day_of_last_month = first_of_this_month - timedelta(days=1)
        start_date = last_day_of_last_month.replace(day=1)
        end_date = last_day_of_last_month
        label = "Last Month"
    elif preset == "custom":
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            start_date = today
        try:
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            end_date = today
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        label = f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}"
    else:  # default today
        preset = "today"
        start_date = today
        end_date = today
        label = "Today"

    # Convert to timezone-aware datetime boundaries
    start_dt_naive = datetime.combine(start_date, time.min)
    end_dt_naive = datetime.combine(end_date, time.max)

    start_datetime = timezone.make_aware(start_dt_naive, tz)
    end_datetime = timezone.make_aware(end_dt_naive, tz)

    return {
        "preset": preset,
        "start_date": start_date,
        "end_date": end_date,
        "start_datetime": start_datetime,
        "end_datetime": end_datetime,
        "label": label,
    }
