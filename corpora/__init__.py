"""corpus 정의 계층.

- `kinds`    : 코드가 소유하는 동작(파싱·임베딩 입력·검색 필터)
- `models`   : DB에 저장되는 corpus 파라미터
- `registry` : SQLite CRUD와 캐시
- `seed`     : 빈 DB 최초 기동 시 삽입되는 `inventions` 한 건
"""
from corpora.kinds import (
    CorpusKind,
    creatable_kinds,
    get_kind,
    kind_of,
)
from corpora.models import (
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    STATUS_UNPUBLISHED,
    CorpusConfig,
    CorpusValidationError,
    rebuild_required_changes,
)
from corpora.registry import (
    CorpusNotFound,
    create,
    delete,
    ensure_seed,
    get,
    get_published,
    invalidate_cache,
    list_all,
    list_published,
    set_active_collection,
    set_status,
    update,
)
from corpora.seed import SEED_CORPUS_ID

__all__ = [
    "CorpusConfig",
    "CorpusKind",
    "CorpusNotFound",
    "CorpusValidationError",
    "SEED_CORPUS_ID",
    "STATUS_DRAFT",
    "STATUS_PUBLISHED",
    "STATUS_UNPUBLISHED",
    "create",
    "creatable_kinds",
    "delete",
    "ensure_seed",
    "get",
    "get_kind",
    "get_published",
    "invalidate_cache",
    "kind_of",
    "list_all",
    "list_published",
    "rebuild_required_changes",
    "set_active_collection",
    "set_status",
    "update",
]
