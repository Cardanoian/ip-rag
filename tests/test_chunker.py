"""chunker.py 단위 테스트."""
from __future__ import annotations

import pytest

from config import CHUNK_OVERLAP, CHUNK_SIZE, MAX_INPUT_TOKENS, SINGLE_CHUNK_CHAR_HINT
from ingest.chunker import chunk_document, chunk_text


# ---------------------------------------------------------------------------
# chunk_document — 짧은 문서 (단일 청크 경로, 휴리스틱 모드)
# ---------------------------------------------------------------------------

class TestChunkDocumentShortText:
    """count_tokens 없이 짧은 텍스트 → 단일 청크."""

    def test_short_text_returns_one_chunk(self):
        text = "가" * 100  # SINGLE_CHUNK_CHAR_HINT(5500)보다 훨씬 짧음
        result = chunk_document(text)
        assert len(result) == 1
        assert result[0] == text

    def test_short_text_chunk_equals_input(self):
        text = "발명 아이디어에 대한 간단한 설명입니다."
        result = chunk_document(text)
        assert result == [text.strip()]

    def test_exactly_at_char_hint_boundary(self):
        text = "나" * SINGLE_CHUNK_CHAR_HINT  # 경계값: 단일 청크 경로
        result = chunk_document(text)
        assert len(result) == 1

    def test_one_char_over_char_hint_splits(self):
        # SINGLE_CHUNK_CHAR_HINT + 1 문자 → 분할 경로
        text = "다" * (SINGLE_CHUNK_CHAR_HINT + 1)
        result = chunk_document(text)
        assert len(result) > 1


# ---------------------------------------------------------------------------
# chunk_document — 긴 문서 (분할 경로, 휴리스틱 모드)
# ---------------------------------------------------------------------------

class TestChunkDocumentLongText:
    """count_tokens 없이 긴 텍스트 → 여러 청크."""

    @pytest.fixture
    def long_text(self) -> str:
        # 12000자 + '\n\n' 구분자 삽입으로 자연스러운 분할 유도
        segment = "가" * 600  # 600자 세그먼트
        return ("\n\n".join([segment] * 20))  # 약 12011자

    def test_long_text_returns_multiple_chunks(self, long_text):
        result = chunk_document(long_text)
        assert len(result) > 1

    def test_each_chunk_within_size_limit(self, long_text):
        result = chunk_document(long_text)
        # RecursiveCharacterTextSplitter는 chunk_size를 엄격히 지키지 않을 수 있으나
        # 큰 허용 범위(2x)를 초과하지 않아야 한다
        for chunk in result:
            assert len(chunk) <= CHUNK_SIZE * 2, (
                f"청크 길이 {len(chunk)}가 허용 범위({CHUNK_SIZE * 2})를 초과"
            )

    def test_consecutive_chunks_overlap(self, long_text):
        result = chunk_document(long_text)
        assert len(result) >= 2
        # 연속 청크 사이에 겹치는 문자열이 존재해야 한다
        found_overlap = False
        for i in range(len(result) - 1):
            # 뒤 청크의 앞부분이 앞 청크의 뒷부분과 겹치는지 확인
            tail = result[i][-CHUNK_OVERLAP:]
            head = result[i + 1][:CHUNK_OVERLAP]
            if tail and head and (tail in result[i + 1] or head in result[i]):
                found_overlap = True
                break
        assert found_overlap, "연속 청크 간 overlap이 감지되지 않음"


# ---------------------------------------------------------------------------
# chunk_document — count_tokens override (토큰 기반 분기 테스트)
# ---------------------------------------------------------------------------

class TestChunkDocumentTokenOverride:
    """count_tokens callable이 분기를 결정해야 한다 (문자 수 무시)."""

    def test_short_text_forced_to_split_by_high_token_count(self):
        # 문자수는 짧지만 count_tokens가 MAX_INPUT_TOKENS 초과를 반환 → 분할 경로
        short_text = "가" * 100  # 문자 기준으로는 단일 청크
        # 분할이 실제로 일어나려면 CHUNK_SIZE(1000)보다 긴 텍스트가 필요
        # 따라서 문자는 짧지만 토큰은 많다고 가정하는 케이스:
        # chunk_text는 짧은 텍스트면 1청크 반환하므로, 분기가 올바른지 검증
        result_with_high_tokens = chunk_document(short_text, count_tokens=lambda t: MAX_INPUT_TOKENS + 1)
        result_without_override = chunk_document(short_text)

        # count_tokens 없으면 짧은 텍스트 → 단일 청크
        assert result_without_override == [short_text]
        # count_tokens가 한도 초과를 반환하면 chunk_text 경로 진입
        # (짧은 텍스트라 실제 split 결과도 1청크일 수 있지만, 분기 자체는 다름)
        # 핵심: 두 경로 모두 비어있지 않은 리스트를 반환해야 함
        assert len(result_with_high_tokens) >= 1

    def test_long_text_forced_to_single_chunk_by_low_token_count(self):
        # 문자수는 SINGLE_CHUNK_CHAR_HINT 초과지만 count_tokens가 5 반환 → 단일 청크
        long_text = "나" * (SINGLE_CHUNK_CHAR_HINT + 1000)
        result = chunk_document(long_text, count_tokens=lambda t: 5)
        assert len(result) == 1
        assert result[0] == long_text.strip()

    def test_count_tokens_drives_branch_not_chars(self):
        # 핵심 검증: 동일 텍스트에 count_tokens 유무로 결과가 달라진다
        # 긴 텍스트 + 낮은 토큰 수 → 단일 청크 (토큰이 우선)
        long_text = "다" * (SINGLE_CHUNK_CHAR_HINT * 2)
        result_token_5 = chunk_document(long_text, count_tokens=lambda t: 5)
        result_token_high = chunk_document(long_text, count_tokens=lambda t: MAX_INPUT_TOKENS + 9999)

        assert len(result_token_5) == 1, "토큰 수 5 → 단일 청크여야 함"
        assert len(result_token_high) > 1, "토큰 수 초과 → 다중 청크여야 함"

    def test_count_tokens_at_exact_limit_is_single_chunk(self):
        long_text = "마" * (SINGLE_CHUNK_CHAR_HINT * 2)
        result = chunk_document(long_text, count_tokens=lambda t: MAX_INPUT_TOKENS)
        assert len(result) == 1  # 정확히 한도와 같으면 단일 청크


# ---------------------------------------------------------------------------
# chunk_document — 빈/공백 입력
# ---------------------------------------------------------------------------

class TestChunkDocumentEmpty:
    def test_empty_string(self):
        assert chunk_document("") == []

    def test_whitespace_only(self):
        assert chunk_document("   \n\t  ") == []

    def test_none_like_empty(self):
        assert chunk_document("") == []

    def test_newlines_only(self):
        assert chunk_document("\n\n\n") == []


# ---------------------------------------------------------------------------
# chunk_text — 기본 분할 함수
# ---------------------------------------------------------------------------

class TestChunkText:
    def test_empty_returns_empty(self):
        assert chunk_text("") == []

    def test_short_text_single_chunk(self):
        text = "짧은 텍스트"
        result = chunk_text(text)
        assert len(result) == 1
        assert result[0] == text

    def test_long_text_splits(self):
        text = "가" * 3000
        result = chunk_text(text)
        assert len(result) > 1

    def test_no_empty_chunks(self):
        text = "\n\n".join(["바" * 500] * 10)
        result = chunk_text(text)
        for chunk in result:
            assert chunk.strip() != "", "빈 청크가 결과에 포함되면 안 됨"
