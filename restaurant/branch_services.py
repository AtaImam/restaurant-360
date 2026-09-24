from restaurant.models import Branch, Restaurant, Table


def get_active_branch(request, restaurant=None):
    """Resolve the active branch for the request context."""
    user = getattr(request, "user", None)

    if user and user.is_authenticated:
        rest = restaurant or getattr(user, "restaurant", None)
        if not rest:
            rest = Restaurant.objects.first()

        if not rest:
            return None

        # Owner / Admin can switch active branch in session
        if user.role in ("owner", "admin") or user.is_superuser:
            branch_id = request.session.get("active_branch_id")
            if branch_id:
                branch = rest.branches.filter(id=branch_id, is_active=True).first()
                if branch:
                    return branch

            # Default to main branch, or first active branch
            branch = (
                rest.branches.filter(is_main=True, is_active=True).first()
                or rest.branches.filter(is_active=True).first()
                or rest.branches.first()
            )
            if branch:
                request.session["active_branch_id"] = branch.id
            return branch

        # Staff users are bound to their assigned branch
        if getattr(user, "branch", None) and user.branch.is_active:
            return user.branch

        # Fallback for staff without assigned branch
        branch = (
            rest.branches.filter(is_main=True, is_active=True).first()
            or rest.branches.filter(is_active=True).first()
            or rest.branches.first()
        )
        return branch

    # Unauthenticated / customer requests
    if restaurant:
        return (
            restaurant.branches.filter(is_main=True, is_active=True).first()
            or restaurant.branches.filter(is_active=True).first()
            or restaurant.branches.first()
        )

    return Branch.objects.filter(is_main=True, is_active=True).first() or Branch.objects.filter(is_active=True).first()


def set_active_branch(request, branch_id):
    """Set active branch in session for owners/admins."""
    user = request.user
    if not user.is_authenticated:
        return None
    rest = getattr(user, "restaurant", None) or Restaurant.objects.first()
    if not rest:
        return None

    branch = rest.branches.filter(id=branch_id, is_active=True).first()
    if branch:
        request.session["active_branch_id"] = branch.id
        return branch
    return None
