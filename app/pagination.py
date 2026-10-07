from dataclasses import dataclass
from math import ceil
from typing import Any, Sequence

from sqlalchemy.orm import Query

DEFAULT_PER_PAGE = 20


@dataclass
class Page:
    items: Sequence[Any]
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, ceil(self.total / self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def _clamp(page: int, total: int, per_page: int) -> int:
    last = max(1, ceil(total / per_page))
    return min(max(1, page), last)


def paginate(query: Query, page: int, per_page: int = DEFAULT_PER_PAGE) -> Page:
    """LIMIT/OFFSET a query. `query` must already be ordered, or pages can
    overlap. A page past the end is clamped to the last page."""
    total = query.order_by(None).count()
    page = _clamp(page, total, per_page)
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return Page(items=items, page=page, per_page=per_page, total=total)


def paginate_list(items: Sequence[Any], page: int, per_page: int = DEFAULT_PER_PAGE) -> Page:
    """Same, for a list already assembled in Python."""
    total = len(items)
    page = _clamp(page, total, per_page)
    start = (page - 1) * per_page
    return Page(items=items[start : start + per_page], page=page, per_page=per_page, total=total)
