from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

from videogen.config import Settings, get_settings
from videogen.models.bible import Character, StoryBible
from videogen.providers.image import ImageProvider, get_image_provider
from videogen.providers.image.base import ImageProviderError, ImageRequest
from videogen.services import cost_service
from videogen.utils.file_store import FileStore
from videogen.utils.redact import scrub_secrets

logger = logging.getLogger(__name__)

__all__ = [
    "CharacterImageError",
    "build_portrait_prompt",
    "generate_all_references",
    "generate_character_image",
    "reference_plan",
]

ProgressCallback = Callable[[int, int, str], None] | None

#: 允许的参考图后缀（和上传接口保持一致）
_ALLOWED_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


class CharacterImageError(RuntimeError):
    """角色图生成失败（消息已脱敏，可直接展示）。"""


def build_portrait_prompt(
    character: Character,
    bible: StoryBible | None = None,
    *,
    aspect_ratio: str = "1:1",
) -> str:
    """把圣经里的角色设定拼成「角色参考图」prompt。

    生成的是**定妆照**而不是剧照：正面、中性表情、干净背景 —— 这样它作为
    视频模型的 `input_references` 才能稳定锁住五官，而不是把某个镜头的光影
    和角度也一起带进去。
    """
    lines: list[str] = []
    who = character.name or character.id
    tail: list[str] = []
    if character.age:
        tail.append(f"{character.age} years old")
    if character.role:
        tail.append(str(character.role))
    subject = f"Character reference portrait of {who}"
    if tail:
        subject += f" ({', '.join(tail)})"
    lines.append(subject)

    look = _one_line(character.visual_prompt) or _one_line(character.description)
    if look:
        lines.append(_base_look(look))
    elif character.personality:
        lines.append("Personality: " + ", ".join(character.personality))

    lines.append(
        "Front-facing head-and-shoulders portrait, neutral calm expression, "
        "even soft studio lighting, plain neutral background, sharp focus on the face, "
        "photorealistic, consistent facial features and hairstyle"
    )
    style = bible.style if bible else None
    if style is not None:
        genre, tone = _one_line(style.genre), _one_line(style.tone)
        if genre or tone:
            lines.append("Visual style: " + " / ".join(p for p in (genre, tone) if p))
    lines.append(
        f"Cinematic character design sheet, {aspect_ratio} composition, "
        "no text, no watermark, no logo, single subject only"
    )
    return "\n".join(lines)


def _one_line(text: str | None) -> str:
    return " ".join(str(text or "").split())


#: 圣经里的外貌描述常常带「人物弧光」（先自卑后逆袭、换造型…）。定妆照要的是
#: **一张稳定的脸**，所以遇到这些转折词就截断，只保留第一阶段的外观。
_ARC_MARKERS = (
    " later,",
    " later ",
    " eventually",
    " transforms into",
    " becomes ",
    " in the end",
    " by the end",
    " ultimately",
)


def _base_look(text: str) -> str:
    """取人物的「基础外观」：截掉弧光转折之后的描述。

    例：`A shy girl in plain clothes. Later, she transforms into a supermodel.`
    → `A shy girl in plain clothes.`

    截完为空（第一句就带转折）时保留原文 —— 宁可描述多，不能没有描述。
    """
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?;])\s+", text):
        lowered = f" {sentence.lower()}"
        if any(marker in lowered for marker in _ARC_MARKERS):
            break
        out.append(sentence.strip())
    base = " ".join(part for part in out if part).strip()
    return base or text


def reference_plan(
    store: FileStore, project_id: str, *, settings: Settings | None = None
) -> dict[str, object]:
    """列出「哪些角色还没有可用的参考图」以及生成一遍的预估花费（不调 API）。"""
    settings = settings or get_settings()
    bible = store.load_bible(project_id)
    if bible is None:
        return {"characters": [], "missing": 0, "total": 0, "estimated_usd": 0.0}

    rows: list[dict[str, object]] = []
    missing = 0
    for character in bible.characters:
        has = _existing_reference(store, project_id, character) is not None
        if not has:
            missing += 1
        rows.append(
            {
                "id": character.id,
                "name": character.name,
                "has_reference": has,
                "reference_image": character.reference_image,
                "prompt_preview": build_portrait_prompt(
                    character, bible, aspect_ratio=settings.image_aspect_ratio
                )[:400],
            }
        )
    return {
        "characters": rows,
        "missing": missing,
        "total": len(rows),
        "estimated_usd": cost_service.estimate_image_usd(missing),
        "price_per_image": settings.image_price_usd,
        "provider": settings.image_provider,
        "model": settings.openrouter_image_model,
    }


def _existing_reference(store: FileStore, project_id: str, character: Character) -> Path | None:
    if not character.reference_image:
        return None
    return store.resolve_artifact(project_id, character.reference_image)


async def generate_character_image(
    store: FileStore,
    project_id: str,
    char_id: str,
    *,
    provider: ImageProvider | None = None,
    force: bool = False,
    use_reference: bool | None = None,
    prompt_override: str = "",
    settings: Settings | None = None,
) -> dict[str, object]:
    """给一个角色生成参考图，落盘 + 写回圣经 + 记账。

    - 已经有参考图且 `force=False` → 直接返回 `skipped=True`（不花钱）
    - `use_reference=True`（或配置 `IMAGE_USE_EXISTING_REFERENCE=true`）时把已有
      参考图作为图生图输入，用来出「同一角色的另一个角度」
    - 预算不足时抛 `CharacterImageError`（不静默超支）
    """
    settings = settings or get_settings()
    bible = store.load_bible(project_id)
    if bible is None:
        raise CharacterImageError("故事圣经不存在，请先生成故事圣经")
    character = bible.get_character(char_id)
    if character is None:
        raise CharacterImageError(f"角色不存在: {char_id}")

    existing = _existing_reference(store, project_id, character)
    if existing is not None and not force:
        return {
            "char_id": char_id,
            "name": character.name,
            "skipped": True,
            "reason": "已有参考图（force=true 可覆盖）",
            "reference_image": character.reference_image,
            "cost_usd": 0.0,
        }

    own_provider = provider is None
    image_provider = provider or get_image_provider()
    want_reference = (
        settings.image_use_existing_reference if use_reference is None else bool(use_reference)
    )
    reference_image = existing if (want_reference and existing is not None) else None

    prompt = _one_line(prompt_override) or build_portrait_prompt(
        character, bible, aspect_ratio=settings.image_aspect_ratio
    )

    # 花钱前先看预算（角色图很便宜，但超支一样是超支）
    cost_service.check_budget(
        cost_service.load_report(store, project_id, settings),
        upcoming_usd=settings.image_price_usd,
        action=f"生成角色图（{character.name or char_id}）",
    )

    refs_dir = store.refs_dir(project_id)
    target = refs_dir / f"{char_id}.png"
    request = ImageRequest(
        subject_id=char_id,
        prompt=prompt,
        output_path=target,
        aspect_ratio=settings.image_aspect_ratio,
        resolution=settings.image_resolution,
        reference_image=reference_image,
    )

    try:
        generated = await image_provider.generate(request)
    except ImageProviderError as exc:
        raise CharacterImageError(
            f"角色图生成失败：{scrub_secrets(str(exc))[:300]}"
        ) from exc
    finally:
        if own_provider:
            await image_provider.aclose()

    saved = Path(generated.path)
    if not saved.is_file() or saved.stat().st_size == 0:
        raise CharacterImageError("图片模型没有产出有效文件")

    # 清掉同名角色的旧后缀文件，避免 refs/ 里堆一堆 char_1.png / char_1.jpg
    for stale in refs_dir.glob(f"{char_id}.*"):
        if stale != saved:
            try:
                stale.unlink()
            except OSError:  # pragma: no cover - 清理失败不影响主流程
                logger.debug("清理旧参考图失败: %s", stale)

    relative = f"refs/{saved.name}"
    index = next(
        (i for i, item in enumerate(bible.characters) if item.id == char_id),
        None,
    )
    if index is not None:
        bible.characters[index] = bible.characters[index].model_copy(
            update={"reference_image": relative}
        )
        store.save_bible(project_id, bible)

    entry = cost_service.image_entry(
        image_provider.name,
        image_provider.model or settings.image_provider,
        1,
        price_usd=settings.image_price_usd,
        actual_usd=generated.actual_cost_usd,
    )
    cost_service.record(store, project_id, entry)
    logger.info(
        "生成角色图 project=%s char=%s model=%s usd=%.4f",
        project_id,
        char_id,
        image_provider.model,
        entry.usd,
    )
    return {
        "char_id": char_id,
        "name": character.name,
        "skipped": False,
        "reference_image": relative,
        "path": str(saved),
        "provider": image_provider.name,
        "model": image_provider.model,
        "prompt": prompt,
        "cost_usd": entry.usd,
        "cost_actual": generated.actual_cost_usd,
        "used_reference": str(reference_image) if reference_image else "",
    }


async def generate_all_references(
    store: FileStore,
    project_id: str,
    *,
    provider: ImageProvider | None = None,
    force: bool = False,
    only: list[str] | None = None,
    concurrency: int = 2,
    on_progress: ProgressCallback = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    """给多个角色批量生成参考图（默认只补缺失的），单个失败不影响其它角色。"""
    settings = settings or get_settings()
    bible = store.load_bible(project_id)
    if bible is None:
        raise CharacterImageError("故事圣经不存在，请先生成故事圣经")

    targets = [c for c in bible.characters if not only or c.id in set(only)]
    if not targets:
        return {"results": [], "generated": 0, "failed": 0, "skipped": 0, "cost_usd": 0.0}
    if not force:
        todo = [c for c in targets if _existing_reference(store, project_id, c) is None]
        skipped = len(targets) - len(todo)
    else:
        todo, skipped = targets, 0

    if not todo:
        return {
            "results": [{"char_id": c.id, "skipped": True} for c in targets],
            "generated": 0,
            "failed": 0,
            "skipped": skipped,
            "cost_usd": 0.0,
        }

    cost_service.check_budget(
        cost_service.load_report(store, project_id, settings),
        upcoming_usd=cost_service.estimate_image_usd(len(todo)),
        action=f"批量生成 {len(todo)} 张角色图",
    )

    own_provider = provider is None
    image_provider = provider or get_image_provider()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: list[dict[str, object]] = []
    done = 0

    async def run_one(character: Character) -> dict[str, object]:
        nonlocal done
        async with semaphore:
            try:
                result = await generate_character_image(
                    store,
                    project_id,
                    character.id,
                    provider=image_provider,
                    force=force,
                    settings=settings,
                )
            except (CharacterImageError, cost_service.BudgetExceeded) as exc:
                logger.warning("角色 %s 参考图失败：%s", character.id, exc)
                return {"char_id": character.id, "name": character.name, "error": str(exc)}
            finally:
                done += 1
                if on_progress:
                    on_progress(done, len(todo), f"角色图 {character.name or character.id}")

        return result

    try:
        results = list(await asyncio.gather(*(run_one(c) for c in todo)))
    finally:
        if own_provider:
            await image_provider.aclose()

    results.extend({"char_id": c.id, "skipped": True} for c in targets if c not in todo)
    generated = sum(1 for r in results if not r.get("skipped") and not r.get("error"))
    failed = sum(1 for r in results if r.get("error"))
    total_usd = round(sum(float(r.get("cost_usd") or 0.0) for r in results), 6)
    return {
        "results": results,
        "generated": generated,
        "failed": failed,
        "skipped": skipped,
        "cost_usd": total_usd,
    }
