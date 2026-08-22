"""외부 디렉터리의 문서를 corpus 문서 디렉터리로 가져온다.

문서는 저장소가 아니라 `DOCS_ROOT/{corpus_id}/` 아래에서 관리한다. 보통은
어드민 화면에서 업로드하지만, 수천 개를 한 번에 넣을 때는 브라우저보다
이 스크립트가 편하다.

    python -m scripts.migrate_docs --corpus inventions --source /경로/문서모음
    python -m scripts.migrate_docs --corpus inventions --source /경로 --dry-run
    python -m scripts.migrate_docs --corpus inventions --source /경로 --mode symlink

symlink 모드는 파일을 복사하지 않고 corpus 디렉터리 자체를 원본으로 연결한다.
디스크가 빠듯하거나 원본을 그대로 두고 싶을 때 쓴다. 컨테이너 배포에서는
볼륨 안에 실제 파일이 있어야 하므로 copy 를 권한다.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import config
import corpora
from corpora.kinds import kind_of


def _iter_source_files(source: Path, extensions: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for extension in extensions:
        files.extend(source.glob(f"*{extension}"))
    return sorted(files)


def migrate(
    corpus_id: str,
    source: Path,
    mode: str = "copy",
    dry_run: bool = False,
) -> dict:
    cfg = corpora.get(corpus_id)
    target = cfg.docs_dir()
    extensions = kind_of(cfg).file_extensions

    if not source.exists():
        raise SystemExit(f"원본 디렉터리가 없습니다: {source}")

    if mode == "symlink":
        if dry_run:
            print(f"[dry-run] {target} → {source.resolve()} 심볼릭 링크 생성")
            return {"mode": "symlink", "linked": str(source.resolve())}
        if target.exists() and not target.is_symlink():
            if any(target.iterdir()):
                raise SystemExit(
                    f"{target} 에 이미 파일이 있습니다. 비운 뒤 다시 실행하세요."
                )
            target.rmdir()
        elif target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source.resolve(), target_is_directory=True)
        print(f"{target} → {source.resolve()} 심볼릭 링크를 만들었습니다.")
        return {"mode": "symlink", "linked": str(source.resolve())}

    files = _iter_source_files(source, extensions)
    stats = {"mode": mode, "copied": 0, "skipped": 0, "total": len(files)}

    if dry_run:
        print(f"[dry-run] {len(files)}개 파일을 {target} 로 복사합니다.")
        return stats

    target.mkdir(parents=True, exist_ok=True)
    for path in files:
        destination = target / path.name
        if destination.exists() and destination.stat().st_size == path.stat().st_size:
            stats["skipped"] += 1
            continue
        shutil.copy2(path, destination)
        stats["copied"] += 1
        if stats["copied"] % 1000 == 0:
            print(f"  {stats['copied']}/{len(files)} 복사...")

    print(
        f"{target} 로 복사 완료: "
        f"{stats['copied']}개 복사, {stats['skipped']}개 건너뜀 (총 {stats['total']}개)"
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.migrate_docs",
        description="기존 docs/ 를 corpus 문서 디렉터리로 옮긴다.",
    )
    parser.add_argument(
        "--corpus",
        default=corpora.SEED_CORPUS_ID,
        help=f"대상 corpus id (기본: {corpora.SEED_CORPUS_ID})",
    )
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="가져올 문서가 들어 있는 디렉터리",
    )
    parser.add_argument(
        "--mode",
        choices=["copy", "symlink"],
        default="copy",
        help="copy는 파일을 복제하고 symlink는 원본을 연결한다 (기본: copy)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제로 옮기지 않고 무엇을 할지만 출력한다.",
    )
    args = parser.parse_args(argv)

    corpora.ensure_seed()
    try:
        migrate(args.corpus, args.source, args.mode, args.dry_run)
    except corpora.CorpusNotFound:
        available = ", ".join(cfg.id for cfg in corpora.list_all())
        print(
            f"오류: 등록되지 않은 corpus — {args.corpus} (등록됨: {available})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
