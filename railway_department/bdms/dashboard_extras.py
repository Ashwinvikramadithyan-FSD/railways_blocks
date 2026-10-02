from django import template

register = template.Library()

_BADGE_MAP = {
    'APPROVED': 'green',
    'ACCEPTED': 'green',
    'ACTIVE': 'green',
    'COMPLETED': 'green',
    'WAITING': 'yellow',
    'RESCHEDULED': 'yellow',
    'REJECTED': 'red',
    'OPEN': 'neutral',
}


@register.filter
def badge_class(status):
    """APPROVED -> 'green', REJECTED -> 'red', etc. Falls back to 'neutral'."""
    return _BADGE_MAP.get(str(status).upper(), 'neutral')


@register.filter
def initials(name):
    """'Ravi Kumar' -> 'RK'. Falls back gracefully on odd input."""
    if not name:
        return '?'
    parts = [p for p in str(name).replace('.', ' ').split() if p]
    letters = ''.join(p[0] for p in parts[:2]).upper()
    return letters or '?'


@register.filter
def get_item(d, key):
    """Look up a dict/queryset-count value by a variable key inside a template."""
    try:
        return d.get(key)
    except AttributeError:
        return None