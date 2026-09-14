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
from videogen.utils.region import region_anchor, region_mentioned, resolve_region

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
    extra: str = "",
    settings: Settings | None = None,
) -> str:
    """把圣经里的角色设定拼成「角色参考图」prompt。

    生成的是**定妆照**而不是剧照：正面、中性表情、干净背景 —— 这样它作为
    视频模型的 `input_references` 才能稳定锁住五官，而不是把某个镜头的光影
    和角度也一起带进去。

    两条硬规则（都是真实跑出来的教训）：

    1. **prompt 里绝不能出现中文**。图片模型会把 prompt 里看到的文字**画进画面**
       （真实事故：中文名「林悦」和风格「都市复仇」被当成海报标题印在图上了）。
       所以名字、`role`、`style.genre/tone` 一律不进来 —— 角色图不需要它们。
    2. **不要说 "sheet" / "poster" / "design"**。这会把画面带成「角色设定海报」
       （带排版和标注）而不是一张干净的证件照。

    第三条规则（v0.17 起）：**外貌要和名字对得上国家的「平均脸」**。名字本身绝不能进
    prompt（会被画成文字），所以这里把名字翻成英文的外貌锚点（`utils/region.py`）：
    中文名 → `Chinese cast … average everyday Chinese face, not mixed-race and not
    Westernised`。不加这句时，模型会按自己的先验往欧美化的五官上靠 ——
    一个中文名角色长一张欧美脸，正是用户报的问题。角色自己的 `visual_prompt`
    或 `IMAGE_PROMPT_SUFFIX` 里已经写明了人种时**不叠加**（避免自相矛盾）。

    `bible` 用来算「整个剧组按哪国来」（一部片子一张脸谱）。
    """
    settings = settings or get_settings()
    lines: list[str] = []
    tail: list[str] = []
    if character.age:
        tail.append(f"{character.age} years old")
    role = _ROLE_EN.get(str(character.role or "").strip())
    if role:
        tail.append(role)
    subject = "Character reference portrait of one person"
    if tail:
        subject += f" ({', '.join(tail)})"
    lines.append(subject)

    look = _one_line(character.visual_prompt) or _one_line(character.description)
    suffix = _one_line(extra)
    # 名字 → 国家 → 该国的「平均脸/主角脸」锚点（已写明人种时不叠加）
    region = resolve_region(
        character,
        bible,
        override=settings.image_region_override,
        default=settings.image_region_default,
        auto=settings.image_region_auto,
    )
    anchor = region_anchor(region.region)
    if anchor and not region_mentioned(look) and not region_mentioned(suffix):
        lines.append(anchor)

    if look:
        lines.append(_base_look(look))
    else:
        # personality 也是中文，绝不能进 prompt（会被画成文字）
        lines.append("An ordinary person with a distinctive, consistent face")

    lines.append(
        "Front-facing head-and-shoulders portrait photograph of that single person, "
        "neutral calm expression, even soft studio lighting, plain seamless neutral "
        "background, sharp focus on the face, photorealistic, identical facial features "
        "and hairstyle"
    )
    lines.append(
        "Plain studio photograph only — absolutely no text, no typography, no letters, "
        "no words, no name, no labels, no captions, no title, no logo, no watermark, "
        "no border, no poster layout, no collage, single subject only"
    )
    lines.append(f"{aspect_ratio} composition")
    if suffix:
        lines.append(suffix)
    return "\n".join(lines)


#: 角色定位转英文：中文 role 出现在 prompt 里会被画成文字（见上面的教训）
_ROLE_EN = {
    "主角": "protagonist",
    "反派": "antagonist",
    "配角": "supporting character",
    "次要反派": "secondary antagonist",
    "导师": "mentor",
    "盟友": "ally",
    "龙套": "background character",
}


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
    orphans: list[str] = []
    for character in bible.characters:
        has = _existing_reference(store, project_id, character) is not None
        orphan = None if has else _orphan_reference(store, project_id, character)
        if orphan is not None:
            orphans.append(character.id)
        if not has and orphan is None:
            missing += 1
        guess = resolve_region(
            character,
            bible,
            override=settings.image_region_override,
            default=settings.image_region_default,
            auto=settings.image_region_auto,
        )
        rows.append(
            {
                "id": character.id,
                "name": character.name,
                "has_reference": has,
                "orphan_file": orphan.name if orphan else "",
                "reference_image": character.reference_image,
                # 这个角色的脸按哪国人来画（名字推断 / 跟剧组 / 人工指定）
                "region": guess.region,
                "region_label": guess.label,
                "region_source": guess.source,
                "region_confidence": round(guess.confidence, 2),
                "prompt_preview": build_portrait_prompt(
                    character,
                    bible,
                    aspect_ratio=settings.image_aspect_ratio,
                    extra=settings.image_prompt_suffix,
                    settings=settings,
                )[:400],
            }
        )
    return {
        "characters": rows,
        "missing": missing,
        #: refs/ 里有文件但圣经没登记（并发写坏 / 手工拷贝）—— 会被**免费**补登记
        "orphans": orphans,
        "total": len(rows),
        "estimated_usd": cost_service.estimate_image_usd(missing),
        "price_per_image": settings.image_price_usd,
        "provider": settings.image_provider,
        "model": settings.openrouter_image_model,
    }


def _existing_reference(store: FileStore, project_id: str, character: Character) -> Path | None:
    """圣经里登记了、且文件真的还在的参考图。"""
    if not character.reference_image:
        return None
    return store.resolve_artifact(project_id, character.reference_image)


def _orphan_reference(store: FileStore, project_id: str, character: Character) -> Path | None:
    """`refs/<char_id>.*` 里有文件，但圣经没登记（并发写坏 / 手工拷贝进来的）。

    这种情况**不能当成「没有参考图」**去重新生成 —— 图已经有了，重新生成是白花钱。
    """
    if _existing_reference(store, project_id, character) is not None:
        return None
    for path in sorted(store.refs_dir(project_id).glob(f"{character.id}.*")):
        if path.is_file() and path.suffix.lower() in _ALLOWED_SUFFIXES:
            return path
    return None


#: 按项目串行化「读圣经 → 改一个角色 → 写回」。批量生成是并发的，如果每个任务各自
#: `load_bible` 再整体 `save_bible`，后写的会把先写的覆盖掉（真实事故：4 张图都生成成功，
#: 圣经里只留下 2 个角色 → 另外 2 个角色在视频阶段拿不到参考图）。
_PROJECT_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(project_id: str) -> asyncio.Lock:
    lock = _PROJECT_LOCKS.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _PROJECT_LOCKS[project_id] = lock
    return lock


async def _write_reference(
    store: FileStore, project_id: str, char_id: str, relative: str
) -> bool:
    """把参考图路径写回圣经（锁内**重新读一遍**再改，只动自己那一个角色）。"""
    async with _lock_for(project_id):
        bible = store.load_bible(project_id)
        if bible is None:
            return False
        index = next(
            (i for i, item in enumerate(bible.characters) if item.id == char_id), None
        )
        if index is None:
            return False
        bible.characters[index] = bible.characters[index].model_copy(
            update={"reference_image": relative}
        )
        store.save_bible(project_id, bible)
        return True


async def adopt_orphan_reference(
    store: FileStore, project_id: str, character: Character
) -> Path | None:
    """把 refs/ 里已存在但没登记的图**免费**补登记，返回它的路径。"""
    orphan = _orphan_reference(store, project_id, character)
    if orphan is None:
        return None
    await _write_reference(store, project_id, character.id, f"refs/{orphan.name}")
    logger.info(
        "补登记已有角色图 project=%s char=%s file=%s", project_id, character.id, orphan.name
    )
    return orphan


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
    if existing is None and not force:
        # 文件已经在 refs/ 里了（并发写坏或手工拷贝）：补登记，绝不重新花钱生成
        adopted = await adopt_orphan_reference(store, project_id, character)
        if adopted is not None:
            return {
                "char_id": char_id,
                "name": character.name,
                "skipped": True,
                "adopted": True,
                "reason": f"refs/ 里已有 {adopted.name}，已免费补登记",
                "reference_image": f"refs/{adopted.name}",
                "cost_usd": 0.0,
            }

    own_provider = provider is None
    image_provider = provider or get_image_provider()
    want_reference = (
        settings.image_use_existing_reference if use_reference is None else bool(use_reference)
    )
    reference_image = existing if (want_reference and existing is not None) else None

    prompt = _one_line(prompt_override) or build_portrait_prompt(
        character,
        bible,
        aspect_ratio=settings.image_aspect_ratio,
        extra=settings.image_prompt_suffix,
        settings=settings,
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
    await _write_reference(store, project_id, char_id, relative)

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
    adopted: list[str] = []
    if not force:
        # 先免费认领 refs/ 里已经存在但没登记的图，避免为同一张图再付一次钱
        for character in targets:
            if _existing_reference(store, project_id, character) is None and (
                await adopt_orphan_reference(store, project_id, character)
            ) is not None:
                adopted.append(character.id)
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
            "adopted": adopted,
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
        #: 免费补登记的角色（文件本来就在 refs/ 里）
        "adopted": adopted,
        "cost_usd": total_usd,
    }
