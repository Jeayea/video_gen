"""从角色名字判斷「这个角色大概是哪国人」，再给出对应的外貌锚点。

要解决的问题（用户报的）：**角色设定和名字要对齐** —— 角色叫中文名就该是一张
中国电视剧里那种「平均脸 / 主角脸」，叫日文名就该是日本人的脸，而不是
「中文名 + 欧美脸」或者整个剧组一半东南亚一半欧美。

为什么需要它：图片/视频模型接到一句 `A 28-year-old woman` 时，会按它自己的
先验去画 —— 那个先验偏向欧美化的五官。名字是唯一**总是存在**的国籍线索，
但名字（尤其中文/日文）又绝不能进 prompt（会被画成画面上的文字，见
`character_image_service` 里的真实事故），所以必须在这里把「名字」翻译成
「英文的外貌锚点」。

判断顺序（先强后弱，都能给出置信度）：

| 线索 | 判据 | 置信度 |
|---|---|---|
| 假名 | 名字里有平假名/片假名 | 0.95 日本 |
| 谚文 | 名字里有谚文 | 0.95 韩国 |
| 文本提示 | 描述/设定里出现「日本」「东京」「首尔」… | 0.9 |
| 日文姓氏 | 名字开头两三个字命中日本姓氏表（田中/佐藤/铃木…） | 0.8 |
| 罗马字姓氏 | 拉丁转写的日文/韩文姓氏表 | 0.75 / 0.7 |
| 中文姓氏 | 名字首字命中中国姓氏表（单字姓） | 0.6 |
| 兜底 | 都没命中（汉字但姓氏不在表里 / 拉丁名） | 0.3，按配置的国家 |

置信度 < 0.5 的角色会**跟随整个剧组**（`project_region`）—— 这就是「对齐」：
一个剧组的绝大多数角色都指向日本，剩下那个没线索的角色也按日本来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "REGION_LABELS",
    "RegionGuess",
    "describe_region",
    "guess_region",
    "normalize_region",
    "project_region",
    "region_anchor",
    "region_conflict",
    "region_mentioned",
    "resolve_region",
]

#: 支持的国家/地区（键用于配置与落盘，值给人看）
REGION_LABELS = {
    "cn": "中国/华语",
    "jp": "日本",
    "kr": "韩国",
    "west": "欧美",
    "other": "其它/不限定",
}

#: 判断用的置信度门槛：低于它就跟整个剧组走
FOLLOW_PROJECT_BELOW = 0.5

#: 各民族/地区的**外貌锚点**（英文，进 prompt）。
#: 措辞刻意用选角行业的说法「typical / average <国家> television drama lead」，
#: 并明确「不要混血/欧美化」—— 模型不加这句就会往欧美化的五官上靠。
REGION_ANCHORS = {
    "cn": (
        "Chinese cast — East Asian features of a typical Chinese television drama lead, "
        "an average everyday Chinese face (straight black hair, dark brown eyes, warm "
        "light-tan skin), not mixed-race and not Westernised"
    ),
    "jp": (
        "Japanese cast — East Asian features of a typical Japanese television drama lead, "
        "an average everyday Japanese face (straight black or dark brown hair, dark brown "
        "eyes, fair-to-light-tan skin), not mixed-race and not Westernised"
    ),
    "kr": (
        "Korean cast — East Asian features of a typical Korean television drama lead, "
        "an average everyday Korean face (straight black hair, dark brown eyes, fair "
        "light skin), not mixed-race and not Westernised"
    ),
    "west": (
        "Western cast — European or North American features of a typical Western TV drama "
        "lead, an average everyday Western face"
    ),
    "other": "",
}

#: 中国常见单字姓（前 100 左右）。注意 `林/金/李/姜/崔/高/森/田` 在中日韩之间会撞，
#: 所以日文和韩文先判，剩下的才当中国姓。
_CN_SURNAMES = {
    "王", "李", "张", "張", "刘", "劉", "陈", "陳", "杨", "楊", "黄", "黃", "赵", "趙",
    "吴", "吳", "周", "徐", "孙", "孫", "马", "馬", "朱", "胡", "郭", "何", "林", "高",
    "罗", "羅", "郑", "鄭", "梁", "谢", "謝", "宋", "唐", "许", "許", "韩", "韓", "冯",
    "馮", "邓", "鄧", "曹", "彭", "曾", "肖", "田", "董", "潘", "袁", "蔡", "蒋", "蔣",
    "余", "于", "杜", "叶", "葉", "程", "魏", "苏", "蘇", "吕", "呂", "丁", "任", "沈",
    "姚", "卢", "盧", "姜", "崔", "钟", "鍾", "谭", "譚", "陆", "陸", "汪", "范", "金",
    "石", "廖", "贾", "賈", "夏", "韦", "韋", "付", "方", "白", "邹", "鄒", "孟", "熊",
    "秦", "邱", "江", "尹", "薛", "闫", "閆", "段", "雷", "侯", "龙", "龍", "史", "陶",
    "黎", "贺", "賀", "顾", "顧", "毛", "郝", "龚", "龔", "邵", "万", "萬", "钱", "錢",
    "严", "嚴", "覃", "武", "戴", "莫", "孔", "向", "汤", "湯", "文", "常", "温", "溫",
    "康", "施", "洪", "翟", "殷", "颜", "顏", "庄", "莊", "倪", "牛", "章", "鲁", "魯",
    "葛", "伍", "申", "尤", "毕", "畢", "聂", "聶", "焦", "柳", "骆", "駱", "祝",
}

#: **中日韩共用**的汉字姓（金=Kim/Jin、李=Lee/Li、林=Lim/Hayashi…）。
#: 这些姓氏单独看**判断不了国籍**，所以给低置信度（0.4，低于 FOLLOW_PROJECT_BELOW），
#: 让这个角色跟随整个剧组 —— 「金敏俊」在韩剧里就是韩国人、在国剧里就是中国人，
#: 靠姓氏本身永远分不出来，靠同剧其他角色反而分得出来。
_SHARED_SURNAMES = {
    "金", "李", "林", "姜", "崔", "张", "張", "韩", "韓", "赵", "趙", "尹", "申", "权", "權",
    "黄", "黃", "安", "宋", "洪", "全", "高", "文", "孙", "孫", "白", "柳", "刘", "劉", "车",
    "車", "具", "闵", "閔", "罗", "羅", "成", "吴", "吳", "郑", "鄭", "朱", "千", "方", "孔",
    "玄", "元", "廉", "卞", "龙", "龍", "卢", "盧", "河", "都", "奇", "马", "馬", "孟", "明",
    "潘", "表", "卜", "史", "苏", "蘇", "玉", "卓", "太", "秦", "蔡", "许", "許", "咸", "延",
    "魏", "殷", "印", "周", "池", "陈", "陳", "秋", "石", "曹", "梁", "沈",
}

#: 韩国特有（或几乎只作韩姓用）的汉字姓 —— 这几个可以把置信度提上去
_KR_SURNAMES_HANZI = {"朴", "裵", "裴", "禹", "鞠", "芮", "昔", "奉", "夫", "庆", "慶", "睦"}

#: 片假名写的韩国姓（日本媒体习惯把韩语人名写成片假名，例如 キム・ジウン）
_KR_SURNAMES_KATAKANA = {
    "キム", "パク", "チェ", "チョン", "カン", "ユン", "チャン", "ソ", "シン", "クォン",
    "ファン", "アン", "ペク", "ペ", "ナム", "リュ", "ユ", "チュン", "コ", "ムン", "ソン", "ホン",
    "イム", "オ", "ハ", "ホ", "チン", "ミン", "ノ", "ソク", "ヨ", "ヨム", "チョ", "チュ", "キ",
    "ト", "ピョン", "シム", "ウ", "ヤン", "クァク", "イ", "リ",
}

#: 日本常见姓氏（汉字）。用 2-3 字的姓氏整体匹配，避免和中文单字姓混淆。
_JP_SURNAMES_KANJI = (
    "佐藤", "铃木", "鈴木", "高桥", "高橋", "田中", "渡边", "渡邊", "渡部", "伊藤", "山本",
    "中村", "小林", "加藤", "吉田", "山田", "佐佐木", "佐々木", "山口", "松本", "井上",
    "木村", "清水", "山崎", "森田", "池田", "桥本", "橋本", "阿部", "石川", "山内", "前田",
    "藤田", "小川", "冈田", "岡田", "后藤", "後藤", "长谷川", "長谷川", "石井", "村上",
    "近藤", "坂本", "远藤", "遠藤", "青木", "藤井", "西村", "福田", "太田", "三浦",
    "冈本", "岡本", "松田", "中川", "中野", "原田", "小野", "竹内", "田村", "金子", "和田",
    "中山", "石田", "上田", "森", "柴田", "酒井", "工藤", "横山", "橫山", "宫崎", "宮崎",
    "宫本", "宮本", "内田", "高木", "安藤", "谷口", "大野", "丸山", "今井", "河野", "藤原",
    "樱井", "櫻井", "野口", "松尾", "野村", "菊地", "佐野", "中岛", "中島", "高田", "小池",
    "石原", "松井", "大冢", "大塚", "高山", "片山", "泽田", "沢田", "冈崎", "岡崎", "宫田",
    "宮田", "小泽", "小澤", "加纳", "加納", "高野", "竹中", "和田", "平野", "杉山", "大西",
    "千叶", "千葉", "小林", "斋藤", "斎藤", "斉藤", "新井", "野中", "小山", "平田", "石桥",
    "石橋", "川口", "大久保", "松冈", "松岡", "增田", "増田", "小松", "大谷", "畠山", "畑中",
)

#: 日本姓氏的罗马字转写
_JP_SURNAMES_ROMAJI = {
    "tanaka", "suzuki", "sato", "satou", "takahashi", "takahasi", "watanabe", "ito", "itou",
    "yamamoto", "nakamura", "kobayashi", "kato", "katou", "yoshida", "yamada", "sasaki",
    "yamaguchi", "matsumoto", "inoue", "kimura", "hayashi", "shimizu", "yamazaki", "mori",
    "ikeda", "hashimoto", "abe", "ishikawa", "yamauchi", "maeda", "fujita", "ogawa", "okada",
    "goto", "gotou", "hasegawa", "ishii", "murakami", "kondo", "kondou", "sakamoto", "endo",
    "endou", "aoki", "fujii", "nishimura", "fukuda", "ota", "oota", "miura", "okamoto",
    "matsuda", "nakagawa", "nakano", "harada", "ono", "oono", "takeuchi", "tamura", "kaneko",
    "wada", "nakayama", "ishida", "ueda", "morita", "shibata", "sakai", "kudo", "kudou",
    "yokoyama", "miyazaki", "miyamoto", "uchida", "takagi", "ando", "taniguchi", "maruyama",
    "imai", "kono", "kouno", "fujiwara", "sakurai", "noguchi", "matsuo", "nomura", "kikuchi",
    "sano", "nakajima", "takada", "koike", "ishihara", "matsui", "takayama", "katayama",
    "sawada", "okazaki", "miyata", "ozawa", "kanno", "takano", "hiraoka", "hirano", "sugiyama",
    "onishi", "chiba", "saito", "saitou", "arai", "nonaka", "koyama", "hirata", "ishibashi",
    "kawaguchi", "okubo", "matsuoka", "masuda", "komatsu", "otani", "hatanaka",
    "yuki", "yui", "akira", "haruka", "sakura", "ren", "souta", "sota", "riku", "hinata",
}

#: 韩国姓氏（罗马字转写，挑与中文拼音区分度高的）
_KR_SURNAMES_ROMAJI = {
    "kim", "park", "choi", "jung", "jeong", "chung", "kang", "yoon", "yun", "jang", "chang",
    "seo", "suh", "shin", "sin", "kwon", "hwang", "ahn", "an", "baek", "paek", "bae", "nam",
    "ryu", "yoo", "yu", "chun", "cheon", "chae", "choe", "koo", "gu", "ku", "moon", "mun",
    "song", "hong", "jeon", "jun", "lim", "im", "oh", "ha", "heo", "hur", "jin", "min",
    "noh", "no", "seok", "suk", "son", "yeo", "yeom", "jo", "joo", "ju", "ki", "do", "byeon",
    "byeun", "sim", "shim", "woo", "wu", "yang", "yeon", "kwak", "gwak", "namgung",
}

#: 中国姓氏的拼音（与韩文/日文表冲突的放后面判）
_CN_SURNAMES_PINYIN = {
    "wang", "li", "zhang", "liu", "chen", "yang", "huang", "zhao", "wu", "zhou", "xu", "sun",
    "ma", "zhu", "hu", "guo", "he", "lin", "gao", "luo", "zheng", "liang", "xie", "song",
    "tang", "han", "cao", "feng", "deng", "peng", "zeng", "xiao", "tian", "dong", "pan",
    "yuan", "cai", "jiang", "yu", "du", "ye", "cheng", "wei", "su", "lv", "lu", "ding", "ren",
    "shen", "yao", "cui", "zhong", "tan", "fan", "jin", "shi", "liao", "jia", "xia", "fu",
    "fang", "bai", "zou", "meng", "xiong", "qin", "qiu", "yin", "xue", "yan", "duan", "lei",
    "hou", "long", "tao", "gu", "mao", "hao", "gong", "shao", "wan", "qian", "dai", "mo",
    "kong", "xiang", "wen", "chang", "kang", "hong", "zhai", "zhuang", "ni",
    "niu", "ge", "you", "bi", "nie", "jiao",
}

#: 描述/设定里的国家线索（中文）
_HINTS = (
    ("jp", ("日本", "日籍", "东京", "大阪", "京都", "北海道", "冲绳", "日语", "日文", "和服",
            "新宿", "涩谷", "涉谷", "银座", "日本桥", "日本人")),
    ("kr", ("韩国", "韩籍", "首尔", "釜山", "韩语", "朝鲜语", "韩剧", "江南区", "韩国人")),
    ("cn", ("中国", "华语", "大陆", "北京", "上海", "广州", "深圳", "重庆", "成都", "杭州",
            "南京", "武汉", "普通话", "中国人", "华侨")),
    ("west", ("美国", "英国", "法国", "德国", "意大利", "西班牙", "俄罗斯", "欧洲", "纽约",
              "伦敦", "巴黎", "洛杉矶", "华尔街", "白人", "欧美", "美国人", "英国人")),
)

_RE_KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\uff66-\uff9f]")
_RE_HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]")
_RE_HANZI = re.compile(r"[\u4e00-\u9fff]")
_RE_LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class RegionGuess:
    """一个角色「大概是哪国人」的判断结果。"""

    region: str
    confidence: float
    #: 判据说明（中文，给人看/排查用），例如 "kana" / "surname:jp" / "hint:jp"
    source: str

    @property
    def confident(self) -> bool:
        """够不够格定这个角色的脸（不够就跟整个剧组走）。"""
        return self.confidence >= FOLLOW_PROJECT_BELOW

    @property
    def label(self) -> str:
        return describe_region(self.region)

    def summary(self) -> dict[str, object]:
        return {
            "region": self.region,
            "label": self.label,
            "confidence": round(self.confidence, 2),
            "source": self.source,
        }


def normalize_region(value: object) -> str:
    """把各种写法（`中国` / `CN` / `chinese` / `日本`…）归一成内部键；不认识返回空串。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in REGION_LABELS:
        return text
    aliases = {
        "china": "cn", "chinese": "cn", "zh": "cn", "cn-zh": "cn", "mandarin": "cn",
        "中国": "cn", "华语": "cn", "中文": "cn", "大陆": "cn",
        "japan": "jp", "japanese": "jp", "ja": "jp", "日本": "jp", "日文": "jp", "日语": "jp",
        "korea": "kr", "korean": "kr", "ko": "kr", "south korea": "kr", "韩国": "kr",
        "韩语": "kr", "朝鲜": "kr",
        "west": "west", "western": "west", "us": "west", "usa": "west", "uk": "west",
        "europe": "west", "european": "west", "caucasian": "west", "american": "west",
        "欧美": "west", "美国": "west", "英国": "west", "欧洲": "west", "白人": "west",
        "other": "other", "其它": "other", "其他": "other", "不限": "other",
    }
    return aliases.get(text, "")


def describe_region(region: str) -> str:
    return REGION_LABELS.get(normalize_region(region), "未判定")


def hiragana_to_katakana(text: str) -> str:
    """平假名转片假名（比较韩姓时统一口径）。"""
    return "".join(
        chr(ord(char) + 0x60) if "\u3041" <= char <= "\u3096" else char for char in text
    )


def _hint_region(text: str) -> RegionGuess | None:
    lowered = str(text or "")
    if not lowered:
        return None
    for region, keywords in _HINTS:
        for keyword in keywords:
            if keyword in lowered:
                return RegionGuess(region=region, confidence=0.9, source=f"hint:{region}")
    return None


def _latin_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z]+", name.lower()) if token]


def guess_region(name: str, *, text: str = "") -> RegionGuess:
    """从名字（可带描述文本）判断国家/地区。

    `text` 传角色的中文描述（description/personality），里面的国名/城市名比名字更硬 ——
    「田中由纪」和「日本人」同时出现时以「日本人」为准（都是日本，但描述能救
    名字看不出国籍的情况，例如「Emma 是英国人」）。
    """
    raw = str(name or "").strip()
    hint = _hint_region(f"{raw} {text}")
    if hint is not None:
        return hint

    if _RE_KANA.search(raw):
        # 片假名里的韩国姓优先（キム・ジウン 不该被当成日本人）
        first = re.split(r"[\s・·　]+", raw.strip())[0]
        if hiragana_to_katakana(first) in _KR_SURNAMES_KATAKANA:
            return RegionGuess("kr", 0.9, f"surname:kr-kana:{first}")
        return RegionGuess("jp", 0.95, "kana")
    if _RE_HANGUL.search(raw):
        return RegionGuess("kr", 0.95, "hangul")

    if _RE_HANZI.search(raw):
        hanzi = "".join(_RE_HANZI.findall(raw))
        # 日本姓氏整体匹配（2-3 字），命中就优先判日本
        for size in (3, 2):
            if len(hanzi) >= size and hanzi[:size] in _JP_SURNAMES_KANJI:
                return RegionGuess("jp", 0.8, f"surname:jp:{hanzi[:size]}")
        if hanzi and hanzi[0] in _KR_SURNAMES_HANZI:
            return RegionGuess("kr", 0.7, f"surname:kr:{hanzi[0]}")
        if hanzi and hanzi[0] in _SHARED_SURNAMES:
            # 共用姓：低置信度 → 这个角色跟随整个剧组（见模块开头说明）
            return RegionGuess("cn", 0.4, f"surname:shared:{hanzi[0]}")
        if hanzi and hanzi[0] in _CN_SURNAMES:
            return RegionGuess("cn", 0.6, f"surname:cn:{hanzi[0]}")
        # 汉字但姓氏不在表里：多半是中文名（表只覆盖常见姓）
        return RegionGuess("cn", 0.3, "hanzi:unknown-surname")

    tokens = _latin_tokens(raw)
    surname = tokens[0] if tokens else ""
    if surname:
        if surname in _JP_SURNAMES_ROMAJI:
            return RegionGuess("jp", 0.75, f"surname:jp:{surname}")
        if surname in _KR_SURNAMES_ROMAJI:
            return RegionGuess("kr", 0.7, f"surname:kr:{surname}")
        if surname in _CN_SURNAMES_PINYIN:
            return RegionGuess("cn", 0.6, f"surname:cn:{surname}")
        # 姓氏不在中日韩的表里 → 拉丁字母本身就是「欧美名」的强线索。
        # （表覆盖不到的生僻拼音姓会在这里被误判成欧美；所以表里额外收了复姓。）
        return RegionGuess("west", 0.6, "latin:unknown-surname")
    return RegionGuess("other", 0.0, "empty")


def project_region(
    bible,
    *,
    override: str = "",
    default: str = "cn",
) -> RegionGuess:
    """整个剧组按哪个国家来（一部片子一张脸谱）。

    权重 = 置信度 × 角色重要性（主角/反派 2，配角 1，龙套 0.5）：
    主角那张脸最重要，但也别让一个高置信度的配角被一群低置信度的角色带偏。
    """
    forced = normalize_region(override)
    if forced:
        return RegionGuess(forced, 1.0, "config:override")

    # 圣经里声明过的国家（用户在生成 bible 时指定的）比投票硬得多
    declared = normalize_region(getattr(bible, "cast_region", ""))
    if declared:
        return RegionGuess(declared, 1.0, "bible:cast_region")

    characters = list(getattr(bible, "characters", []) or [])
    if not characters:
        fallback = normalize_region(default) or "cn"
        return RegionGuess(fallback, 0.2, "default")

    weights = {"主角": 2.0, "反派": 2.0, "次要反派": 1.5, "配角": 1.0, "龙套": 0.5}
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    declared_other = False
    for character in characters:
        guess = guess_region(
            getattr(character, "name", ""),
            text=f"{getattr(character, 'description', '')} "
            f"{getattr(character, 'visual_prompt', '')} "
            f"{getattr(character, 'region', '')}",
        )
        explicit = normalize_region(getattr(character, "region", ""))
        if explicit:
            guess = RegionGuess(explicit, 1.0, "character:region")
        if guess.region == "other":
            declared_other = declared_other or explicit == "other"
            continue
        role = str(getattr(character, "role", "") or "").strip()
        weight = guess.confidence * weights.get(role, 1.0)
        scores[guess.region] = scores.get(guess.region, 0.0) + weight
        reasons.setdefault(guess.region, guess.source)

    if not scores:
        if declared_other:
            # 圣经明确写了「不限定」（例如翻译小说没判出国籍）：不加人种锚点，
            # 不能退回默认国家（真实事故：卡夫卡的布拉格一家被画成了中国脸）
            return RegionGuess("other", 1.0, "character:region")
        fallback = normalize_region(default) or "cn"
        return RegionGuess(fallback, 0.2, "default")
    best = max(scores, key=lambda key: scores[key])
    total = sum(scores.values())
    return RegionGuess(best, round(scores[best] / total, 2), f"cast:{reasons[best]}")


def resolve_region(
    character,
    bible=None,
    *,
    override: str = "",
    default: str = "cn",
    auto: bool = True,
) -> RegionGuess:
    """定一个角色该按哪国人画脸。

    优先级（高的在前）：
    1. `IMAGE_REGION_OVERRIDE`（配置里强制整片）
    2. 角色自己的 `region` 字段（圣经 LLM 写、人工也能改）
    3. 名字 + 描述推断得**够自信**（≥ 0.5）
    4. 跟随整个剧组（`project_region`）—— 这就是「对齐」：
       「金敏俊」这种分不出国籍的名字，在国剧里是中国人、在韩剧里是韩国人

    `auto=False` 直接返回空 region（等于关掉这个功能）。
    """
    if not auto:
        return RegionGuess("", 0.0, "disabled")
    forced = normalize_region(override)
    if forced:
        return RegionGuess(forced, 1.0, "config:override")

    explicit = normalize_region(getattr(character, "region", ""))
    if explicit:
        return RegionGuess(explicit, 1.0, "character:region")

    own = guess_region(
        getattr(character, "name", ""),
        text=f"{getattr(character, 'description', '')} {getattr(character, 'visual_prompt', '')}",
    )
    declared = normalize_region(getattr(bible, "cast_region", ""))
    if declared:
        # 声明过就照声明来（用户比启发式可靠）；但名字明显指向别的国家时要**报出来** ——
        # 这多半是圣经把名字起错了，让用户能看见并去改名/改 region
        note = ""
        if own.confident and own.region != declared:
            note = f"(名字像{describe_region(own.region)}:{own.source})"
        return RegionGuess(declared, 1.0, f"bible:cast_region{note}")
    if own.confident:
        return own
    project = project_region(bible, default=default)
    return RegionGuess(
        region=project.region,
        confidence=project.confidence,
        source=f"follow-cast:{own.source}->{project.region}",
    )


def region_conflict(character, declared: str) -> str:
    """名字指向的国家和「声明的国家」不一致时返回一句可执行的提示，否则空串。"""
    target = normalize_region(declared)
    if not target:
        return ""
    if normalize_region(getattr(character, "region", "")):
        return ""  # 已经单独指定过（例如剧情里的外国人），不算冲突
    own = guess_region(
        getattr(character, "name", ""),
        text=f"{getattr(character, 'description', '')} {getattr(character, 'visual_prompt', '')}",
    )
    if not own.confident or own.region == target or own.region == "other":
        return ""
    return (
        f"名字「{getattr(character, 'name', '')}」看起来是{describe_region(own.region)}名，"
        f"但整片按{describe_region(target)}来 —— 建议改名字或单独给这个角色填 region"
    )


def region_anchor(region: str) -> str:
    """某国的外貌锚点（英文，进 prompt）；不认识的返回空串。"""
    return REGION_ANCHORS.get(normalize_region(region), "")


#: 描述里已经写了这些词，就说明外貌已经定过调，不要再叠锚点（免得自相矛盾）
_MENTION_MARKERS = (
    "chinese", "japanese", "korean", "asian", "east asian", "caucasian", "western",
    "european", "american", "white", "black", "african", "latina", "hispanic",
    "middle eastern", "south asian", "mixed-race", "biracial", "mongolian", "tibetan",
    "uyghur", "thai", "vietnamese", "filipino",
)


def region_mentioned(text: str) -> bool:
    """这段英文描述里是不是已经交代了人种/地区。"""
    lowered = f" {str(text or '').lower()} "
    return any(marker in lowered for marker in _MENTION_MARKERS)
