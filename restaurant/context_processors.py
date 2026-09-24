from restaurant.branch_services import get_active_branch


def active_branch_context(request):
    """Expose active branch and available branches to all templates."""
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        active_branch = get_active_branch(request)
        restaurant = getattr(user, "restaurant", None)
        branches = []
        if restaurant:
            branches = list(restaurant.branches.filter(is_active=True).order_by("-is_main", "name"))
        return {
            "active_branch": active_branch,
            "user_branches": branches,
            "can_switch_branch": (user.role in ("owner", "admin") or user.is_superuser),
        }
    return {
        "active_branch": None,
        "user_branches": [],
        "can_switch_branch": False,
    }
