# -*- coding: utf-8 -*-
"""HandFont - 手写字体生成器"""
import sys, os, json, shutil, struct, sqlite3, zlib
from array import array
# Qt 绑定: 优先 PySide6(Qt6, 只有 64 位); 32 位 Python / Win7 上装不了 PySide6,
# 只能退到 PySide2(Qt5)。两者的类名一致, 只有极少数方法不同, 下面用 _evt_pos() 等兜住,
# 其余代码一份跑两边, 不给 32 位单独留分支。
try:
    from PySide6.QtCore import (
        Qt, QPointF, QEvent, QSize, Signal, QTimer, QTranslator, QLibraryInfo
    )
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QListWidget, QListWidgetItem,
        QLineEdit, QPushButton, QHBoxLayout, QVBoxLayout, QSplitter,
        QLabel, QFileDialog, QMessageBox, QSlider, QSizePolicy, QProgressDialog,
        QCheckBox, QListView, QStyledItemDelegate, QStyle, QAbstractItemView,
        QComboBox, QTabWidget
    )
    from PySide6.QtGui import (
        QPainter, QPen, QColor, QFont, QFontMetricsF, QPainterPath, QPainterPathStroker,
        QPixmap, QCursor, QPalette, QTransform, QIcon, QPolygonF
    )
    QT_BINDING = "PySide6"
except ImportError:              # 32 位 Python 3.8 之类的环境
    from PySide2.QtCore import (
        Qt, QPointF, QEvent, QSize, Signal, QTimer, QTranslator, QLibraryInfo
    )
    from PySide2.QtWidgets import (
        QApplication, QMainWindow, QWidget, QListWidget, QListWidgetItem,
        QLineEdit, QPushButton, QHBoxLayout, QVBoxLayout, QSplitter,
        QLabel, QFileDialog, QMessageBox, QSlider, QSizePolicy, QProgressDialog,
        QCheckBox, QListView, QStyledItemDelegate, QStyle, QAbstractItemView,
        QComboBox, QTabWidget
    )
    from PySide2.QtGui import (
        QPainter, QPen, QColor, QFont, QFontMetricsF, QPainterPath, QPainterPathStroker,
        QPixmap, QCursor, QPalette, QTransform, QIcon, QPolygonF
    )
    QT_BINDING = "PySide2"
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont


def _app_dir():
    """可读写的数据目录(工程库等)。
    源码运行时=脚本目录; 打包成 exe 后=exe 所在目录 —— onefile 模式下 __file__
    指向临时解包目录, 工程库若跟着放那儿, 每次启动都会是一个空工程。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _res_dir():
    """只读资源目录: PyInstaller 用 --add-data 打进去的文件在 sys._MEIPASS 下"""
    return getattr(sys, "_MEIPASS", None) or _app_dir()


def _evt_pos(event):
    """取鼠标/数位板事件的坐标: Qt6 用 event.position() (QPointF),
    Qt5 只有 event.pos() (QPoint), 这里统一成 QPointF。"""
    pos = getattr(event, "position", None)
    if pos is not None:
        return pos()
    p = event.pos()
    return QPointF(float(p.x()), float(p.y()))


def _set_font_families(font, families):
    """按优先级设置字体族; Qt5 早期版本没有 setFamilies, 退回只设第一个"""
    setter = getattr(font, "setFamilies", None)
    if setter is not None:
        setter(list(families))
    else:
        font.setFamily(families[0])

# 半角 ASCII 不属于 GB2312 字符集(区 3 收的是全角 ！＂＃), 单独做成一张附录卡片,
# 不并进 GB2312 表, 否则整表的区位位置会整体偏移 94 位, 无法与编码表对照
ASCII_CHARS = [chr(c) for c in range(33, 127)]


def _gb2312_chars():
    """枚举 GB2312 全部双字节字符, 严格按区位表升序(0xA1A1 → 0xF7FE):
    区 1-9 符号 682 个(标点/拉丁/希腊/俄文/拼音/注音/制表符等),
    区 16-55 一级汉字 3755 个, 区 56-87 二级汉字 3008 个。"""
    out = []
    for hi in range(0xA1, 0xFF):
        for lo in range(0xA1, 0xFF):
            try:
                out.append(bytes((hi, lo)).decode("gb2312"))
            except UnicodeDecodeError:
                continue      # 该区位是空的
    return out


GB2312_CHARS = _gb2312_chars()


def gb2312_position(ch):
    """返回字符在 GB2312 表中的 (区号, 位号); 不属于 GB2312 时返回 None"""
    try:
        b = ch.encode("gb2312")
    except UnicodeEncodeError:
        return None
    if len(b) != 2:
        return None
    return b[0] - 0xA0, b[1] - 0xA0


# ---------- 分区统计: 让用户看得见"还差多少字" ----------
ZONE_GROUPS = [
    (1, 1, "区1 标点"), (2, 2, "区2 序号"), (3, 3, "区3 全角ASCII"),
    (4, 4, "区4 平假名"), (5, 5, "区5 片假名"), (6, 6, "区6 希腊文"),
    (7, 7, "区7 俄文"), (8, 8, "区8 拼音注音"), (9, 9, "区9 制表符"),
    (16, 55, "一级汉字"), (56, 87, "二级汉字"),
]


def zone_group_of(ch):
    """返回字符所属的分区名; 不是 GB2312 字符返回 None"""
    pos = gb2312_position(ch)
    if not pos:
        return None
    z = pos[0]
    for lo, hi, name in ZONE_GROUPS:
        if lo <= z <= hi:
            return name
    return None


def gb2312_coverage(saved_chars):
    """统计 GB2312 各分区的书写进度 -> {分区名: [已写, 总数]}"""
    total = {name: 0 for _, _, name in ZONE_GROUPS}
    for ch in GB2312_CHARS:
        g = zone_group_of(ch)
        if g:
            total[g] += 1
    done = {name: 0 for _, _, name in ZONE_GROUPS}
    for ch in saved_chars:
        g = zone_group_of(ch)
        if g and g in done:
            done[g] += 1
    return {name: [done[name], total[name]] for name in total}


# ---------- 字频优先排序 ----------
FREQ_FILE = os.path.join(_res_dir(), "char_freq.txt")


def _load_freq_order():
    """读取字频排序文件: 汉字按语料频次从高到低排列, 文件未收录的罕见字按编码顺序补在末尾。
    数据由 wordfreq 中文语料生成; 文件缺失/损坏时返回 None, 调用方回退编码顺序。"""
    try:
        with open(FREQ_FILE, "r", encoding="utf-8") as fp:
            raw = fp.read()
    except OSError:
        return None
    valid = set(GB2312_CHARS)
    seen, out = set(), []
    for ch in raw:
        if ch in valid and ch not in seen:
            seen.add(ch)
            out.append(ch)
    if len(out) < 1000:          # 文件明显不完整, 不要用
        return None
    # 文件里漏掉的"汉字"按编码顺序补在末尾 (符号不归这个文件管, 由调用方另行前置)
    for ch in GB2312_CHARS:
        if ch not in seen and gb2312_position(ch)[0] >= 16:
            out.append(ch)
            seen.add(ch)
    return out


_FREQ_CACHE = []


def gb2312_by_freq():
    """区 1-9 符号保持编码顺序(符号没有频率概念), 汉字按使用频率排列"""
    if _FREQ_CACHE:
        return _FREQ_CACHE[0]
    order = _load_freq_order()
    if order is None:
        result = list(GB2312_CHARS)
    else:
        symbols = [c for c in GB2312_CHARS if gb2312_position(c)[0] < 16]
        result = symbols + order
    _FREQ_CACHE.append(result)
    return result


# 符号区(区 1-9)字符数, 也是两种排序模式下"第一个汉字"所在行
GB2312_SYMBOL_COUNT = sum(1 for c in GB2312_CHARS if gb2312_position(c)[0] < 16)

UNITS_PER_EM = 1000          # 画布预览用的字身单位; 导出的字身单位取模板的 unitsPerEm
REF_CELL_PX = 280.0          # 笔画粗细公式的参考格子边长(像素) —— 画布与导出共用
DEFAULT_STROKE_WIDTH = 18    # 「笔画粗细」滑块默认值
DEFAULT_SORT_BY_FREQ = True  # 「排序」默认: 字频优先(常用字在前, 写到 1000 字即覆盖日常文本约 92%)
DEFAULT_SLIM_EXPORT = True   # 导出默认: 精简模式, 只打包已经写过的字

GHOST_FILL = 0.86                                    # 汉字底字占格子边长的比例(拉丁底字按字体度量画, 不用这个值)
GHOST_COLOR = QColor(120, 120, 120, 40)              # 描红底色: 中性灰, 与黑色笔迹、红色活动框都不冲突，透明度40（0-255），数值越大越黑

# 格子线: 外框深而粗, 内部参考线浅而细 —— 方格边界一眼可辨, 又不会跟笔迹抢眼睛。
# 原来的外框是 1px 的 215 灰, 在白底上几乎看不见, 学生写字容易出格。
GRID_EDGE_COLOR = QColor(104, 104, 98)               # 外框: 深灰(不是纯黑, 免得像笔迹)
GRID_EDGE_WIDTH = 1.8                                # 外框宽度(px); 活动格红框 2.0 仍比它抢眼
GRID_LINE_COLOR = QColor(184, 184, 177)              # 内部参考线: 米字格对角线/中线、四线三格 x 高线
GRID_LINE_WIDTH = 1.1                                # 内部参考线宽度(px)

# 导出时对轮廓做 Douglas-Peucker 抽稀, 删掉"几乎共线"的冗余点。
# 单位是模板 em 的千分之一(与坐标同单位): 2.0 = 字身的 0.2%, 48px 字号下不足 0.1px, 视觉无差异。
# 实测 1592 字: 10.0MB → 3.8MB, 墨迹差 0.8%。设为 0 可完全关闭抽稀(恢复原始体积)。
SIMPLIFY_EPS = 2.0

# 布尔并集后丢弃面积小于此值的轮廓 (单位: em 的千分之一, 同坐标)。
# simplified() 会产生若干面积恰为 0 的退化碎屑, 必须清掉;
# 真实笔画最小的是光标点一下的圆点, 面积也在 500 以上, 阈值 1 绝对安全。
UNION_MIN_AREA = 1.0

# ---- 字形在字身中的纵向位置与行高 (导出 TTF 专用, 不影响画布) ----
# 整个米字格在字身里占 1.0 em 高, 这里定格子底边落在基线的什么位置:
# -0.15 表示格子底边在基线下 0.15 em, 顶边在基线上 0.85 em, 格心正好落在 0.35 em 处。
# 实测宋体/黑体的字面中心在基线上方 0.36 em、楷体 0.30 em, 本设置与它们基本齐平。
GLYPH_BOTTOM_EM = -0.15
# 行框上下沿(em)。行高 = ASCENT_EM - DESCENT_EM = 1.00 em, 与米字格完全重合:
# 宋体/黑体/楷体都是 1.00 em, 模板华文楷体是 1.12 em(显松, 字会显小)。
# 圆头笔画会略微超出格线(约 0.04 em = 12 磅字号下半个像素), GDI 实测不裁切。
ASCENT_EM = 0.85
DESCENT_EM = -0.15

# ---- 拉丁字符(ASCII)专用: 四线三格 ----
# 米字格给的是"居中/对称"参考(对角线 + 中线), 汉字靠它判断结构;
# 拉丁字母靠的是基线、x 高和每个字母自己的字宽, 两者不是一回事 —— 用米字格写英文,
# 字母底部会浮在基线上方 0.05~0.21em, 且降部(p g y q j)在格子里没有空间。
# 所以 ASCII 字符单独用一套纵向映射: 格子覆盖 LATIN_BOTTOM_EM ~ LATIN_TOP_EM, 总高仍是 1.0em,
# 基线落在格子 75% 高度处。顶边 0.75 / 底边 -0.25 是按实际拉丁度量定的
# (Times 的 b/i 上沿 0.745em, g/p 下沿 -0.23em), 两侧刚好留一点点余量。
LATIN_BOTTOM_EM = -0.25      # 格子底边 = 降部线
LATIN_TOP_EM = 0.75          # 格子顶边 = 升部线(b d h k l) / 大写线
LATIN_X_HEIGHT_EM = 0.48     # x 高线; 汉字字面约 0.9em, 0.48 是 CJK 字体里拉丁的常规比例
LATIN_ADV_PAD_EM = 0.03      # 字宽虚线向内留的空隙, 免得相邻字母粘连
# 描红底字用衬线体: 与楷体手写的中英混排最协调; 依次尝试, 都没有就退回系统默认
LATIN_GHOST_FONTS = ["Times New Roman", "Segoe UI", "Arial"]
# 轮廓缓存的拉丁标志位: 拉丁字符换了纵向映射, 旧轮廓不能复用, 汉字继续用旧键(不必重算)
LATIN_OUTLINE_BIT = 1 << 48


def is_latin_char(ch):
    """ASCII 可见字符走四线三格; 汉字、全角符号、空格仍走米字格"""
    return len(ch) == 1 and ord(ch) < 128 and not ch.isspace()


def _latin_ny(y_em):
    """拉丁纵向映射: 字体坐标(em) → 格子内的归一化高度(0 = 顶边, 1 = 底边)"""
    return (LATIN_TOP_EM - y_em) / (LATIN_TOP_EM - LATIN_BOTTOM_EM)


def stroke_width_em(avg_pressure, width_max, em):
    """笔画宽度, 单位 em。画布预览和导出 TTF 共用这一个公式, 保证所见即所得。
    画布里的像素宽度是 (1.5 + 压感*滑块值) * 格子边长/280;
    这里换算成 em: 像素 * em / 280。"""
    px_at_ref = 1.5 + avg_pressure * width_max
    return max(px_at_ref * em / REF_CELL_PX, em * 0.004)   # 兜底防 0 宽


# 模板字体: 依次尝试常见楷体, 都不存在则报错提示。
# 优先选 em 精度高的楷体(1000/2048), 避免 256em 模板导致轮廓量化锯齿。
TEMPLATE_CANDIDATES = [
    r"C:\Windows\Fonts\STKAITI.TTF",    # 华文楷体, upem=1000 (首选)
    r"C:\Windows\Fonts\simkai.ttf",     # 楷体(简体), upem=256 (备选)
    r"C:\Windows\Fonts\simfang.ttf",    # 仿宋, upem=256
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑, upem=2048
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体, upem=256
]


def _template_font_path():
    """模板字体路径; 画布(取拉丁字宽)和导出(取度量)共用同一个选择逻辑"""
    return next((p for p in TEMPLATE_CANDIDATES if os.path.exists(p)), None)


_LATIN_ADV_CACHE = {}


def latin_advance(ch):
    """ASCII 字符在模板字体里的字宽(单位 em)。
    画布上的字宽虚线要用它: 拉丁字母宽度差别极大(i 0.23 / m 0.77 / W 0.89),
    不画出来就只能凭手感, 写出来自然疏密不均。读不到就按 0.5em 兜底。"""
    if not _LATIN_ADV_CACHE:
        adv = {}
        path = _template_font_path()
        if path:
            try:
                f = TTFont(path, lazy=True)      # 只读 cmap/hmtx, 不展开 2.5 万字轮廓
                em = f["head"].unitsPerEm
                hmtx = f["hmtx"]
                cmap = f.getBestCmap() or {}
                for cp in range(32, 127):
                    gn = cmap.get(cp)
                    if gn and gn in hmtx.metrics:
                        adv[chr(cp)] = hmtx.metrics[gn][0] / em
            except Exception as e:
                print("读取模板拉丁字宽失败(按 0.5em 兜底):", e)
        _LATIN_ADV_CACHE.update(adv)
    return _LATIN_ADV_CACHE.get(ch, 0.5)


def _simplify_poly(poly, eps):
    """Douglas-Peucker 轮廓抽稀(首尾点固定), 删除视觉上冗余的共线/近平共线点。
    描边轮廓的大头是圆头圆弧和直线上的密集采样点, 抽稀后体积可降到 1/3 而肉眼无差。
    eps=0 时原样返回, 完全跳过。"""
    if eps <= 0:
        return poly
    n = len(poly)
    if n < 3:
        return poly
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        x1, y1 = poly[a]
        x2, y2 = poly[b]
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = poly[i]
            if l2 == 0:
                d = ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
            else:
                t = ((px - x1) * dx + (py - y1) * dy) / l2
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                d = ((px - (x1 + t * dx)) ** 2 + (py - (y1 + t * dy)) ** 2) ** 0.5
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(poly, keep) if k]

PROJECT_FILE = os.path.join(_app_dir(), "handfont_project.json")
PROJECT_DB = os.path.join(_app_dir(), "handfont_project.db")


# ---------------- 工程数据存储 ----------------
# 旧做法是把整个工程序列化成一份 JSON 反复重写: 5000 字时文件已 55 MB,
# 每点一次「保存」就要重新序列化 + 覆盖写一遍, 实测 4.2 秒, 且随字数线性变慢。
# 现在每个字单独存一行, 保存只改这一行, 耗时与总字数无关 (实测约 6 毫秒)。

def _pack_strokes(strokes):
    """笔画 → 紧凑二进制。
    格式: b'HF1' + 笔画数(u16) + 每笔点数(u32 × n) + 点坐标(float32 × 3 × 总点数)
    坐标是 [0,1] 的归一化值, float32 精度约 1e-7 em, 换算到 280px 格子上是 0.00003 像素,
    肉眼看不出差别, 体积却只有 JSON 文本的 1/5。"""
    counts = array("I", (len(s) for s in strokes))
    flat = array("f")
    for s in strokes:
        for pt in s:
            flat.append(pt[0])
            flat.append(pt[1])
            flat.append(pt[2] if len(pt) > 2 else 0.5)   # 老数据可能只有 x,y
    return b"HF1" + struct.pack("<H", len(strokes)) + counts.tobytes() + flat.tobytes()


def _unpack_strokes(blob):
    """_pack_strokes 的逆过程, 还原成 [[(x, y, 压力), ...], ...]"""
    if blob[:3] != b"HF1":
        raise ValueError("未知的笔画数据格式")
    n = struct.unpack_from("<H", blob, 3)[0]
    off = 5
    counts = struct.unpack_from("<%dI" % n, blob, off) if n else ()
    off += 4 * n
    total = sum(counts)
    flat = array("f")
    flat.frombytes(blob[off:off + 12 * total])
    # zip(it, it, it) 一次切出三个分量, 比逐点索引快得多
    it = iter(flat)
    triples = list(zip(it, it, it))
    out, k = [], 0
    for c in counts:
        out.append(triples[k:k + c])
        k += c
    return out


# ---------------- 轮廓缓存 ----------------
# 导出的真正耗时不在写字, 而在每个字的「描边 + 布尔并集」: 实测每字 52 毫秒,
# 5000 字要 4 分多钟, 其中约 3/4 花在 QPainterPath.simplified() 上。
# 而轮廓只跟笔画、粗细、em 精度有关, 写完就不再变 —— 所以算完就存起来,
# 之后导出直接取用, 全量从 4 分钟降到约 3 秒。
# TTF 坐标本来就是整数(em 单位), 用 int16 存是零损失: 取出来的就是最终写进字体的那些值。
# 体积每字约 500 字节(zlib 后), 6887 字实测主库 17.9 MB -> 21.7 MB (增加约 21%)。
OUTLINE_CACHE_VER = 1      # 改了描边/并集/抽稀算法就把这个加 1, 旧缓存自动失效重算


def _outline_key(stroke_width, em, latin=False):
    """缓存失效键: 粗细、em 精度、算法版本, 任一项变了都不能复用旧轮廓。
    拉丁字符另加一个标志位(纵向映射不同), 且不改变汉字已有的键, 避免全量重算。"""
    key = (int(OUTLINE_CACHE_VER) << 32) | (int(round(stroke_width)) << 16) | int(em)
    return key | (LATIN_OUTLINE_BIT if latin else 0)


def _pack_outline(contours):
    """轮廓 → 紧凑二进制 + zlib。
    格式: b'HFO1' + 轮廓数(u16) + 每轮廓点数(u16 × n) + 坐标(int16 × 2 × 总点数)
    坐标是字体单位(如 1000 em 下 0..1000), int16 装得下且与 TTF 存储精度完全一致。"""
    counts = array("H", (len(c) for c in contours))
    flat = array("h")
    for c in contours:
        for x, y in c:
            flat.append(int(x))
            flat.append(int(y))
    raw = b"HFO1" + struct.pack("<H", len(contours)) + counts.tobytes() + flat.tobytes()
    return zlib.compress(raw, 6)


def _unpack_outline(blob):
    """_pack_outline 的逆过程 → [[(x, y), ...], ...]"""
    raw = zlib.decompress(blob)      # 必须解压后再校验: 压缩后的头 4 字节是 zlib 头, 不是魔数
    if raw[:4] != b"HFO1":
        raise ValueError("未知的轮廓缓存格式")
    n = struct.unpack_from("<H", raw, 4)[0]
    off = 6
    counts = struct.unpack_from("<%dH" % n, raw, off) if n else ()
    off += 2 * n
    flat = array("h")
    flat.frombytes(raw[off:off + 4 * sum(counts)])
    it = iter(flat)
    pairs = list(zip(it, it))
    out, k = [], 0
    for c in counts:
        out.append(pairs[k:k + c])
        k += c
    return out


class ProjectStore:
    """工程存储: SQLite 单表, 一个字一行。
    保存 = 只 REPLACE 该行 (毫秒级, 与总字数无关);
    事务保证原子性, 不会因为写盘中断把整个工程写坏。"""

    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(path)
        # WAL: 提交不必每次把整个库 fsync 一遍, 单行提交只需毫秒级
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("CREATE TABLE IF NOT EXISTS glyphs("
                         "ch TEXT PRIMARY KEY, strokes BLOB NOT NULL)")
        # 轮廓缓存两列 + 拉丁字宽一列: 老库在这里自动升级, 不用手工迁移
        cols = {row[1] for row in self.con.execute("PRAGMA table_info(glyphs)")}
        if "outline" not in cols:
            self.con.execute("ALTER TABLE glyphs ADD COLUMN outline BLOB")
        if "out_key" not in cols:
            self.con.execute("ALTER TABLE glyphs ADD COLUMN out_key INTEGER")
        if "adv" not in cols:
            self.con.execute("ALTER TABLE glyphs ADD COLUMN adv REAL")
        self.con.commit()

    def load(self):
        """读出全部字形。单行损坏只跳过那一行, 不牵连整个工程。"""
        data = {}
        for ch, blob in self.con.execute("SELECT ch, strokes FROM glyphs"):
            try:
                data[ch] = _unpack_strokes(blob)
            except Exception as e:
                print(f"跳过损坏的字形 {ch!r}:", e)
        return data

    def load_adv(self):
        """读出全部自定义拉丁字宽 {字符: em 值}。没设过的字不在字典里(用模板默认)。"""
        return {ch: adv for ch, adv in
                self.con.execute("SELECT ch, adv FROM glyphs WHERE adv IS NOT NULL")}

    def put(self, ch, strokes, adv=None):
        """写一个字(已有则覆盖), adv 是该字的最终字宽(None = 模板默认)。
        REPLACE 是删旧行再插新行, 没写到的列一律变 NULL —— 轮廓缓存跟着清掉,
        重写的字下次导出必重算轮廓; 字宽则由调用方显式传入, 重写不丢。"""
        with self.con:
            self.con.execute("REPLACE INTO glyphs(ch, strokes, adv) VALUES(?,?,?)",
                             (ch, _pack_strokes(strokes), adv))

    def set_adv(self, ch, adv):
        """只改一个字的字宽, 不碰笔画; adv=None 表示恢复模板默认。
        该字还没写过(没有行)时返回 False —— 没写过就没地方存,
        调用方先记在内存里, 等写完保存时随笔画一起落库。"""
        if self.con.execute("SELECT 1 FROM glyphs WHERE ch=?", (ch,)).fetchone() is None:
            return False
        with self.con:
            self.con.execute("UPDATE glyphs SET adv=? WHERE ch=?", (adv, ch))
        return True

    def delete(self, ch):
        """删掉一个字, 让它回到"未写"状态; 本来就没有也算成功。
        轮廓缓存和自定义字宽存在同一行里, 跟着一起删掉, 不会留下孤立数据。"""
        with self.con:
            self.con.execute("DELETE FROM glyphs WHERE ch=?", (ch,))

    def put_many(self, items, progress=None):
        """批量写入; progress(done, total) 用于迁移时报告进度"""
        total = len(items)
        if not total:
            return
        if progress is None:
            with self.con:
                self.con.executemany("REPLACE INTO glyphs(ch, strokes) VALUES(?,?)",
                                     ((ch, _pack_strokes(v)) for ch, v in items))
            return
        step = max(1, total // 100)
        for i in range(0, total, step):
            chunk = items[i:i + step]
            with self.con:
                self.con.executemany("REPLACE INTO glyphs(ch, strokes) VALUES(?,?)",
                                     ((ch, _pack_strokes(v)) for ch, v in chunk))
            progress(i + len(chunk), total)

    def get_outline(self, ch, key):
        """取该字的轮廓缓存; 没算过、参数对不上或数据坏了都返回 None (调用方现算)"""
        row = self.con.execute(
            "SELECT outline, out_key FROM glyphs WHERE ch=?", (ch,)).fetchone()
        if not row or row[0] is None or row[1] != key:
            return None
        try:
            return _unpack_outline(row[0])
        except Exception:
            return None          # 缓存坏了就当没有, 不影响导出

    def put_outlines(self, items):
        """批量存轮廓缓存: items = [(ch, key, contours), ...], 一个事务写完。
        只 UPDATE 不 INSERT —— 轮廓缓存永远依附于已有的笔画行。"""
        if not items:
            return
        with self.con:
            self.con.executemany(
                "UPDATE glyphs SET outline=?, out_key=? WHERE ch=?",
                ((_pack_outline(c), k, ch) for ch, k, c in items))

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM glyphs").fetchone()[0]

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass


class CharCellDelegate(QStyledItemDelegate):
    """把每个格子画成带细边框的方格, 只显示字符本身 (不显示 U+ 码位)
    颜色随系统明暗主题自适应; 已写的字正常色, 未写的字灰显"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.side = 32          # 由 CharGridView 按列宽同步
        # 已写字符的判定集合, 由 CharGridView.set_written_source() 指向
        # MainWindow.saved_data —— 只做 in 判断, 不复制数据, 存盘后立刻生效
        self.written = {}

    def sizeHint(self, option, index):
        # 让绘制区域铺满整个网格单元, 否则只在网格里居中画一小块
        return QSize(self.side, self.side)

    def paint(self, painter, option, index):
        r = option.rect
        state = option.state
        pal = option.palette
        dark_bg = pal.color(QPalette.ColorRole.Base).lightness() < 128
        selected = bool(state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(r, QColor(38, 79, 120) if dark_bg else QColor(198, 226, 250))
        elif state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(r, QColor(58, 62, 68) if dark_bg else QColor(242, 246, 250))
        else:
            painter.fillRect(r, pal.color(QPalette.ColorRole.Base))

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QColor(72, 72, 76) if dark_bg else QColor(232, 232, 226))
        painter.drawRect(r.adjusted(0, 0, -1, -1))
        painter.restore()

        ch = index.data(Qt.ItemDataRole.UserRole) or ""
        written = ch in self.written
        painter.save()
        f = QFont(option.font)
        f.setPixelSize(max(10, int(min(r.width(), r.height()) * 0.55)))
        painter.setFont(f)
        if selected:
            pen = QColor(235, 244, 255) if dark_bg else QColor(12, 44, 84)
            if not written:
                pen.setAlpha(140)      # 选中格底色重, 未写的字用半透明保留灰显感
        elif written:
            pen = QColor(226, 226, 226) if dark_bg else QColor(24, 24, 24)
        else:
            # 未写: 明显淡一档, 但仍能看清是哪个字, 便于扫视"还差哪些"
            pen = QColor(112, 112, 116) if dark_bg else QColor(160, 160, 164)
        painter.setPen(pen)
        painter.drawText(r, Qt.AlignmentFlag.AlignCenter, ch)
        painter.restore()


class CharGridView(QListWidget):
    """固定 10 列的字符表格: 用 IconMode 让格子按网格排列, 列宽随窗口宽度自动均分"""
    COLUMNS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)                       # 满 10 列自动换行
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)  # 禁止拖动图标位置
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._delegate = CharCellDelegate(self)
        self.setItemDelegate(self._delegate)
        self._side = -1
        self._syncing = False

    def set_written_source(self, db):
        """告诉绘制代理"哪些字已经写了"(直接引用工程字典, 不复制内容)。
        字典变化后调用 viewport().update() 重绘即可看到颜色更新。"""
        self._delegate.written = db

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_grid()

    def sync_grid(self):
        """把可用宽度均分成 10 等份, 每格取正方形。
        不能用视口宽度算: 视口宽度会随竖向滚动条的出现/消失变化一个滚动条宽,
        而"要不要出滚动条"又取决于格子排完后的内容高度 —— 两者互相影响成回路,
        94 个 ASCII 字符(10 行)的高度刚好卡在临界值时, 格子边长会在 27↔29 之间
        反复横跳, 切到那张卡片就能看见格子不停重排闪动。
        这里按"控件宽度 - 边框 - 固定的滚动条预留"算, 宽度不再随滚动条变化, 回路断开。"""
        if self._syncing:
            return                      # doItemsLayout 可能再次触发 resize, 防止递归
        sb = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent,
                                      None, self.verticalScrollBar())
        avail = self.width() - 2 * self.frameWidth() - sb
        if avail <= 0:
            return
        side = max(16, avail // self.COLUMNS)
        if side == self._side:
            return
        self._syncing = True
        try:
            self._side = side
            d = self.itemDelegate()
            if isinstance(d, CharCellDelegate):
                d.side = side
            self.setGridSize(QSize(side, side))
            self.doItemsLayout()
        finally:
            self._syncing = False


class CharCard(QWidget):
    """左侧的一张字符卡片: 书写进度 + 字符表格
    (两张卡片放进 QTabWidget, 单击上方卡片名即可切换, 一次只显示一张)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(6, 6, 6, 6)
        box.setSpacing(3)
        self.prog = QLabel("—")
        self.prog.setWordWrap(True)     # 左栏收窄时第二行放不下就折成三行, 不截断
        f = QFont("Microsoft YaHei", 8)
        self.prog.setFont(f)
        # 高度保底一行, 其余交给 wordWrap 的 heightForWidth 自动撑高
        fm = self.prog.fontMetrics()
        self.prog.setMinimumHeight(fm.height() + 2)
        box.addWidget(self.prog)
        self.view = CharGridView()
        self.view.setFont(QFont("Microsoft YaHei", 10))
        box.addWidget(self.view, stretch=1)


class GlyphCanvas(QWidget):
    # 方向键微调: 平移当前格内已写的笔画, 直接改归一化坐标
    # (这样画布显示和导出 TTF 用的是同一份数据, 不会出现"看着动了导出没动")
    nudgeRequested = Signal(float, float)   # dx, dy (归一化, 右/下为正)
    resetRequested = Signal()               # Ctrl+0 复位本格偏移
    statusChanged = Signal()                # 当前字/偏移变了, 通知底部状态栏刷新
    advEdited = Signal(str)                 # 字宽虚线被拖完/双击复位了, 主窗口负责落库

    NUDGE_LIMIT = 0.15      # 允许超出米字格边界的最大比例, 防止把字拖飞
    ADV_GRAB_PX = 7         # 鼠标离字宽虚线多近算"抓住"了它(像素)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)   # 需要键盘焦点才能收方向键
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # 2x2 米字格, 画布最小 200x200 → 单格最小 100x100, 低分辨率小屏也能缩到很小
        self.setMinimumSize(200, 200)
        self.cell_size = 280
        self.offset_x = 0
        self.offset_y = 0
        self.cells = [[], [], [], []]
        self.cell_offsets = [[0.0, 0.0] for _ in range(4)]  # 每格累计偏移, 仅用于显示/复位
        self.active_cell = 0
        self.current = None
        self.current_char = ""
        self._last_pt = None
        self.stroke_width_max = DEFAULT_STROKE_WIDTH
        self._cursor_is_dot = False
        self.dot_cursor = self._make_dot_cursor()
        # 描红底字: 每个格子里都用淡色画出当前要写的字, 方便照着笔画写
        self.show_ghost = True
        self._ghost_key = None
        self._ghost_path = None
        # 自定义拉丁字宽 {字符: em}: 直接引用 MainWindow 的字典, 拖虚线改的就是它
        self._adv_map = None
        self._adv_drag = None       # 拖拽中: (字符, 拖拽起始格子的左边缘 x)
        self._cursor_is_h = False    # 光标是否已切成"左右拖动"形状

    def _make_dot_cursor(self):
        """黑色圆点光标, 带白色描边保证压在笔画上也看得见"""
        pm = QPixmap(32, 32)
        pm.fill(QColor(0, 0, 0, 0))
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(QColor(255, 255, 255, 210))
        qp.drawEllipse(10, 10, 12, 12)
        qp.setBrush(QColor(15, 15, 15))
        qp.drawEllipse(13, 13, 6, 6)
        qp.end()
        return QCursor(pm, 16, 16)

    def _in_grid(self, px, py):
        """判断坐标是否落在 2x2 米字格区域内"""
        lx = px - self.offset_x
        ly = py - self.offset_y
        return 0 <= lx < 2 * self.cell_size and 0 <= ly < 2 * self.cell_size

    def _update_cursor(self, px, py):
        # 贴着字宽虚线: 换成左右箭头, 提示这条线可以拖
        if self._near_adv_line(px, py):
            if not self._cursor_is_h:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                self._cursor_is_h = True
                self._cursor_is_dot = False
            return
        if self._cursor_is_h:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._cursor_is_h = False
        want_dot = self._in_grid(px, py)
        if want_dot != self._cursor_is_dot:
            self.setCursor(self.dot_cursor if want_dot else Qt.CursorShape.ArrowCursor)
            self._cursor_is_dot = want_dot

    def enterEvent(self, event):
        p = _evt_pos(event)
        self._update_cursor(p.x(), p.y())
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._cursor_is_dot = False
        self._cursor_is_h = False
        super().leaveEvent(event)

    def resizeEvent(self, event):
        # 正方形 2x2, 在画布内居中
        side = min(self.width(), self.height())
        self.cell_size = side // 2
        self.offset_x = (self.width() - 2*self.cell_size) // 2
        self.offset_y = (self.height() - 2*self.cell_size) // 2
        self.update()

    def set_char(self, ch, saved_strokes=None):
        self.current_char = ch
        self._adv_drag = None      # 切字时撤销进行中的字宽拖拽, 免得宽度算到新字头上
        self.cells = [[], [], [], []]
        self.cell_offsets = [[0.0, 0.0] for _ in range(4)]
        self.current = None
        self.active_cell = 0
        if saved_strokes:
            self.cells[0] = [list(s) for s in saved_strokes]
        self.update()
        self.statusChanged.emit()

    def set_stroke_width(self, w):
        self.stroke_width_max = w
        self.update()

    def set_show_ghost(self, on):
        """淡显底字开关"""
        self.show_ghost = bool(on)
        self.update()

    def set_adv_source(self, adv_map):
        """告诉画布"哪些字设过自定义字宽"(直接引用工程字典, 不复制)。
        拖动字宽虚线会直接写进这个字典, 落库由 MainWindow 在拖拽结束时负责。"""
        self._adv_map = adv_map

    def _char_adv(self):
        """当前字的字宽(em): 自定义优先, 没设过用模板默认"""
        if self._adv_map and self.current_char in self._adv_map:
            return self._adv_map[self.current_char]
        return latin_advance(self.current_char)

    def _adv_cell_at(self, px, py):
        """px/py 落在哪个格子里, 返回该格 (原点x, 原点y); 不在格子里返回 None"""
        lx = px - self.offset_x
        ly = py - self.offset_y
        if not (0 <= lx < 2 * self.cell_size and 0 <= ly < 2 * self.cell_size):
            return None
        col = min(1, int(lx // self.cell_size))
        row = min(1, int(ly // self.cell_size))
        return (self.offset_x + col * self.cell_size,
                self.offset_y + row * self.cell_size)

    def _near_adv_line(self, px, py):
        """鼠标是否贴在字宽虚线上(只有拉丁字符有这条线)"""
        if self._adv_drag or not self._is_latin() or self._adv_map is None:
            return bool(self._adv_drag)
        adv = self._char_adv()
        if not (0.05 < adv < 0.995):
            return False
        cell = self._adv_cell_at(px, py)
        if cell is None:
            return False
        return abs(px - (cell[0] + self.cell_size * adv)) <= self.ADV_GRAB_PX

    def _is_latin(self):
        """当前字是 ASCII 时用四线三格, 否则用米字格"""
        return is_latin_char(self.current_char)

    def _ghost(self, cs):
        """当前字符的淡色字形轮廓 (以格子左上角为原点), 按格子边长缓存。
        汉字用楷体制形——笔画起收与手写最接近, 照着写比黑体容易;
        拉丁字符另走 _ghost_latin(按基线定位, 不做包围盒归一化)。"""
        ch = self.current_char
        if is_latin_char(ch):
            return self._ghost_latin(cs)
        key = (ch, cs)
        if key == self._ghost_key:
            return self._ghost_path
        self._ghost_key = key
        self._ghost_path = None
        if not ch or ch.isspace():
            return None                      # 空格、全角空格没有笔画可描
        f = QFont("KaiTi")
        f.setPixelSize(max(12, int(cs * 0.92)))
        path = QPainterPath()
        path.addText(0.0, 0.0, f, ch)
        br = path.boundingRect()
        if br.width() <= 0.5 or br.height() <= 0.5:
            return None
        # 按字形实际外框等比缩放并居中: 「一」和「口」的包围盒差很多, 不归一化会大小不一
        target = cs * GHOST_FILL
        s = min(target / br.width(), target / br.height())
        tr = QTransform()
        tr.translate(cs / 2.0, cs / 2.0)
        tr.scale(s, s)
        tr.translate(-br.center().x(), -br.center().y())
        self._ghost_path = tr.map(path)
        return self._ghost_path

    def _ghost_latin(self, cs):
        """拉丁底字: 不做包围盒归一化, 而是把 x 高对齐到格线、基线钉在基线上。
        这一点很关键——归一化会把 i / a / m / H 撑成一样大, 等于在教人"写满格",
        相对高度(b 比 a 高、p 向下伸)全丢掉了。"""
        ch = self.current_char
        key = ("L", ch, cs)
        if key == self._ghost_key:
            return self._ghost_path
        self._ghost_key = key
        self._ghost_path = None
        if not ch or ch.isspace():
            return None
        f = QFont()
        _set_font_families(f, LATIN_GHOST_FONTS)
        probe = 200
        f.setPixelSize(probe)
        fm = QFontMetricsF(f)
        xh = fm.xHeight()
        if xh <= 1.0:
            return None                      # 字体没有 x 高度量, 放弃底字(不影响书写)
        f.setPixelSize(max(8, int(round(probe * (LATIN_X_HEIGHT_EM * cs) / xh))))
        y_base = cs * _latin_ny(0.0)         # 基线位置(格子 75% 高度处)
        path = QPainterPath()
        path.addText(0.0, y_base, f, ch)
        br = path.boundingRect()
        if br.width() <= 0.5 or br.height() <= 0.5:
            return None
        tr = QTransform()
        tr.translate(cs * LATIN_ADV_PAD_EM - br.left(), 0.0)   # 起笔贴左线内侧
        self._ghost_path = tr.map(path)
        return self._ghost_path

    def _to_norm(self, px, py):
        lx = px - self.offset_x
        ly = py - self.offset_y
        col = max(0, min(1, int(lx // self.cell_size)))
        row = max(0, min(1, int(ly // self.cell_size)))
        idx = row * 2 + col
        nx = max(0.0, min(1.0, (lx - col*self.cell_size) / self.cell_size))
        ny = max(0.0, min(1.0, (ly - row*self.cell_size) / self.cell_size))
        return idx, nx, ny

    def _to_norm_active(self, px, py):
        """笔画进行中专用: 坐标始终锁定在当前活动格子的局部坐标系。
        修复"笔尖碰到米字格边线, 笔迹跳到格子另一侧"的 bug ——
        之前每一点都按笔尖当下所在的格子换算局部坐标, 越过边线的一瞬
        col/row 翻转, 坐标从 0.99 跳到 0.01。现在只按活动格子计算并
        钳制到 [0,1], 出界笔迹贴边不跳格。"""
        ax = self.offset_x + (self.active_cell % 2) * self.cell_size
        ay = self.offset_y + (self.active_cell // 2) * self.cell_size
        nx = max(0.0, min(1.0, (px - ax) / self.cell_size))
        ny = max(0.0, min(1.0, (py - ay) / self.cell_size))
        return nx, ny

    def undo_stroke(self):
        if self.cells[self.active_cell]:
            self.cells[self.active_cell].pop()
            self.update()

    def clear_active(self):
        self.cells[self.active_cell] = []
        self.current = None
        self.cell_offsets[self.active_cell] = [0.0, 0.0]   # 没笔画了, 偏移归零
        self.update()
        self.statusChanged.emit()

    def clear_all(self):
        self.cells = [[], [], [], []]
        self.cell_offsets = [[0.0, 0.0] for _ in range(4)]
        self.current = None
        self.update()
        self.statusChanged.emit()

    def get_active_strokes(self):
        return self.cells[self.active_cell]

    # ---------- 方向键微调 ----------
    def has_active_strokes(self):
        return bool(self.cells[self.active_cell]) and self.current is None

    @staticmethod
    def _clamp_delta(d, lo, hi):
        if hi < lo:
            return 0.0
        return max(lo, min(d, hi))

    def nudge_active_cell(self, dx, dy):
        """平移当前格内全部已写笔画。返回实际生效的 (dx, dy), 无变化返回 None。
        以整体包围盒为约束做限位, 不是逐点截断 —— 逐点截断会把字压扁变形。"""
        if not self.has_active_strokes():
            return None
        strokes = self.cells[self.active_cell]
        xs = [p[0] for s in strokes for p in s]
        ys = [p[1] for s in strokes for p in s]
        if not xs:
            return None
        lo, hi = -self.NUDGE_LIMIT, 1.0 + self.NUDGE_LIMIT
        adx = self._clamp_delta(dx, lo - min(xs), hi - max(xs))
        ady = self._clamp_delta(dy, lo - min(ys), hi - max(ys))
        if adx == 0.0 and ady == 0.0:
            return None
        for s in strokes:
            for i, (nx, ny, pr) in enumerate(s):
                s[i] = (nx + adx, ny + ady, pr)
        off = self.cell_offsets[self.active_cell]
        off[0] += adx
        off[1] += ady
        self.update()
        self.statusChanged.emit()
        return adx, ady

    def reset_active_offset(self):
        """把本格累计的偏移整体退回原位"""
        off = self.cell_offsets[self.active_cell]
        if abs(off[0]) < 1e-9 and abs(off[1]) < 1e-9:
            return None
        dx, dy = -off[0], -off[1]
        off[0] = off[1] = 0.0
        for s in self.cells[self.active_cell]:
            for i, (nx, ny, pr) in enumerate(s):
                s[i] = (nx + dx, ny + dy, pr)
        self.update()
        self.statusChanged.emit()
        return dx, dy

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = 0.005                      # 默认 0.5% 字宽
            if mods & Qt.KeyboardModifier.ShiftModifier:
                step = 0.025                  # Shift 大步 2.5%
            elif mods & Qt.KeyboardModifier.ControlModifier:
                step = 0.001                   # Ctrl 细调 0.1%
            dx = -step if key == Qt.Key.Key_Left else (step if key == Qt.Key.Key_Right else 0.0)
            dy = -step if key == Qt.Key.Key_Up else (step if key == Qt.Key.Key_Down else 0.0)
            if self.has_active_strokes():
                self.nudgeRequested.emit(dx, dy)
                event.accept()
                return
        elif key == Qt.Key.Key_0 and (mods & Qt.KeyboardModifier.ControlModifier):
            self.resetRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def tabletEvent(self, event):
        p = _evt_pos(event)
        self._update_cursor(p.x(), p.y())
        pressure = event.pressure()
        etype = event.type()
        if etype == QEvent.Type.TabletPress:
            if pressure <= 0:
                event.accept(); return
            self.setFocus(Qt.FocusReason.MouseFocusReason)   # 选中格子后方向键才生效
            idx, nx, ny = self._to_norm(p.x(), p.y())
            self.active_cell = idx
            self.current = [(nx, ny, pressure)]
            self._last_pt = (nx, ny)
        elif etype == QEvent.Type.TabletMove and self.current is not None:
            ax = self.offset_x + (self.active_cell % 2)*self.cell_size
            ay = self.offset_y + (self.active_cell // 2)*self.cell_size
            dx = p.x() - ax - self._last_pt[0]*self.cell_size
            dy = p.y() - ay - self._last_pt[1]*self.cell_size
            if dx*dx + dy*dy < 2.0:
                event.accept(); return
            nx, ny = self._to_norm_active(p.x(), p.y())
            self.current.append((nx, ny, pressure))
            self._last_pt = (nx, ny)
        elif etype == QEvent.Type.TabletRelease and self.current is not None:
            if len(self.current) >= 2:
                self.cells[self.active_cell].append(self.current)
            self.current = None
            self._last_pt = None
        event.accept()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.current is None:
            self.setFocus(Qt.FocusReason.MouseFocusReason)   # 选中格子后方向键才生效
            p = _evt_pos(event)
            # 先判"抓住字宽虚线": 抓住就是拖宽度, 不再起笔画
            # (数位笔的 tablet 事件总是被 accept, 不会派生鼠标事件, 用笔写不受影响;
            #  这条线只认真实鼠标 —— 拖线是偶尔的调整动作, 用鼠标, 落笔是高频动作, 用笔)
            if self._is_latin() and self._adv_map is not None:
                adv = self._char_adv()
                cell = self._adv_cell_at(p.x(), p.y())
                if cell is not None and 0.05 < adv < 0.995 \
                        and abs(p.x() - (cell[0] + self.cell_size * adv)) <= self.ADV_GRAB_PX:
                    self._adv_drag = (self.current_char, cell[0])
                    self._update_cursor(p.x(), p.y())
                    return
            idx, nx, ny = self._to_norm(p.x(), p.y())
            self.active_cell = idx
            self.current = [(nx, ny, 0.5)]
            self._last_pt = (nx, ny)

    def mouseMoveEvent(self, event):
        p = _evt_pos(event)
        if self._adv_drag is not None:
            ch, ax = self._adv_drag
            adv = (p.x() - ax) / self.cell_size
            # 钳制到 0.10~0.99em, 0.005em 一档(状态栏两位小数稳定显示)
            adv = max(0.10, min(0.99, round(adv * 200) / 200.0))
            self._adv_map[ch] = adv
            self.statusChanged.emit()
            self.update()
            return
        self._update_cursor(p.x(), p.y())
        if self.current is not None and event.buttons() & Qt.MouseButton.LeftButton:
            p = _evt_pos(event)
            nx, ny = self._to_norm_active(p.x(), p.y())
            dx = nx - self._last_pt[0]
            dy = ny - self._last_pt[1]
            if dx*dx + dy*dy < 0.00005:
                return
            self.current.append((nx, ny, 0.5))
            self._last_pt = (nx, ny)
            self.update()

    def mouseReleaseEvent(self, event):
        if self._adv_drag is not None:
            ch = self._adv_drag[0]
            self._adv_drag = None
            self.advEdited.emit(ch)     # 拖完了, 让主窗口把宽度写进库
            self._update_cursor(_evt_pos(event).x(), _evt_pos(event).y())
            return
        if self.current is not None and len(self.current) >= 2:
            self.cells[self.active_cell].append(self.current)
        self.current = None
        self._last_pt = None
        self.update()

    def mouseDoubleClickEvent(self, event):
        """双击字宽虚线: 恢复模板默认宽度。连点两下的过程里虚线可能被拖动过,
        无所谓 —— 这里的恢复一定发生在最后, 以这里为准。"""
        if event.button() == Qt.MouseButton.LeftButton and self._adv_drag is None \
                and self._is_latin() and self._adv_map is not None \
                and self.current is None:
            p = _evt_pos(event)
            if self._near_adv_line(p.x(), p.y()) \
                    and self.current_char in self._adv_map:
                ch = self.current_char
                self._adv_map.pop(ch)
                self.advEdited.emit(ch)
                self.statusChanged.emit()
                self.update()
                return
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.fillRect(self.rect(), QColor(250, 250, 245))
        cs = self.cell_size
        for idx in range(4):
            col = idx % 2; row = idx // 2
            ox = self.offset_x + col*cs
            oy = self.offset_y + row*cs
            self._draw_one_cell(qp, ox, oy, cs, idx)

    def hint_text(self):
        """底部状态栏用的提示文字: 当前字 + 方向键微调说明 + 当前格的偏移。
        (原来直接画在画布顶部, 会压住米字格干扰书写, 改由 MainWindow 显示)"""
        hint = f"当前: {self.current_char}"
        if is_latin_char(self.current_char):
            # 拉丁字符的要点与汉字不同, 放在最前面: 标签被截断时这句也要留得住
            if self._adv_map and self.current_char in self._adv_map:
                hint += (f"（四线三格 · 字宽 {self._adv_map[self.current_char]:.2f}"
                         f" / 默认 {latin_advance(self.current_char):.2f}"
                         " · 双击虚线恢复）")
            else:
                hint += "（四线三格 · 字母压基线 · 拖右虚线可改字宽）"
        off = self.cell_offsets[self.active_cell]
        if abs(off[0]) > 1e-9 or abs(off[1]) > 1e-9:
            # 横向右为正; 纵向按字体坐标系显示, 向上为正
            hint += f"　偏移 {off[0]*100:+.1f}% / {-off[1]*100:+.1f}%"
        # 微调说明放最后: 标签窄到要截断时, 先保住偏移数值, 被截的是这段固定说明
        hint += "　方向键微调 · Shift 大步 · Ctrl 细调 · Ctrl+0 复位"
        return hint

    def _draw_one_cell(self, qp, ox, oy, cs, idx):
        # 先画淡色底字, 网格线与笔迹压在上面, 线条不会被底字糊掉
        if self.show_ghost:
            ghost = self._ghost(cs)
            if ghost is not None:
                qp.save()
                qp.translate(ox, oy)
                qp.fillPath(ghost, GHOST_COLOR)
                qp.restore()
        if self._is_latin():
            self._draw_latin_grid(qp, ox, oy, cs, idx)
        else:
            self._draw_mi_grid(qp, ox, oy, cs)
        if idx == self.active_cell:
            hp = QPen(QColor(220, 80, 80)); hp.setWidth(2)
            qp.setPen(hp)
            qp.drawRect(ox+1, oy+1, cs-3, cs-3)
        for stroke in self.cells[idx]:
            self._draw_stroke(qp, stroke, ox, oy, cs)
        if idx == self.active_cell and self.current:
            self._draw_stroke(qp, self.current, ox, oy, cs)

    def _draw_mi_grid(self, qp, ox, oy, cs):
        """米字格: 外框 + 两条对角线 + 两条中线, 给出居中和对称的参考"""
        ep = QPen(GRID_EDGE_COLOR); ep.setWidthF(GRID_EDGE_WIDTH)
        qp.setPen(ep)
        qp.drawRect(ox, oy, cs-1, cs-1)
        pen = QPen(GRID_LINE_COLOR); pen.setWidthF(GRID_LINE_WIDTH)
        qp.setPen(pen)
        qp.drawLine(ox, oy, ox+cs-1, oy+cs-1)
        qp.drawLine(ox+cs-1, oy, ox, oy+cs-1)
        qp.drawLine(ox+cs//2, oy, ox+cs//2, oy+cs-1)
        qp.drawLine(ox, oy+cs//2, ox+cs-1, oy+cs//2)

    def _draw_latin_grid(self, qp, ox, oy, cs, idx):
        """四线三格: 顶边(升部/大写线)、x 高线、基线、底边(降部线) + 该字母的字宽虚线。
        横线只给高度参考, 纵向居中的中线对字母没有任何意义, 所以不画。"""
        ep = QPen(GRID_EDGE_COLOR); ep.setWidthF(GRID_EDGE_WIDTH)
        qp.setPen(ep)
        qp.drawRect(ox, oy, cs-1, cs-1)                       # 顶边 / 底边
        pen = QPen(GRID_LINE_COLOR); pen.setWidthF(GRID_LINE_WIDTH)
        qp.setPen(pen)
        y_xh = oy + cs * _latin_ny(LATIN_X_HEIGHT_EM)
        qp.drawLine(QPointF(ox, y_xh), QPointF(ox+cs-1, y_xh))  # x 高线
        # 基线用略深一点的冷灰蓝, 比其他线更醒目: 所有字母都要压在这条线上
        bp = QPen(QColor(130, 148, 168)); bp.setWidthF(1.5)
        qp.setPen(bp)
        y_bl = oy + cs * _latin_ny(0.0)
        qp.drawLine(QPointF(ox, y_bl), QPointF(ox+cs-1, y_bl))
        # 字宽虚线: 这条线右侧就是下一个字母的地盘, 越线就会与后一个字粘连。
        # 鼠标贴线变左右箭头即可拖动改宽度; 设过自定义宽度后颜色加深并在顶端标数值
        adv = self._char_adv()
        if 0.05 < adv < 0.995:
            custom = self._adv_map is not None and self.current_char in self._adv_map
            ap = QPen(QColor(226, 143, 68) if custom else QColor(203, 176, 132))
            ap.setWidthF(2.0 if custom else 1.0)
            ap.setStyle(Qt.PenStyle.DashLine)
            qp.setPen(ap)
            x_adv = ox + cs * adv
            qp.drawLine(QPointF(x_adv, oy+1), QPointF(x_adv, oy+cs-1))
            if custom and idx == self.active_cell:
                # 顶端小三角 + 宽度数值: "这条线被你改过了"一眼可见(只标活动格)
                qp.save()                       # 画完恢复画刷, 别把后面画框的填充也染了
                qp.setBrush(QColor(226, 143, 68))
                qp.drawPolygon(QPolygonF([QPointF(x_adv, oy+9), QPointF(x_adv-4, oy+2),
                                          QPointF(x_adv+4, oy+2)]))
                qp.restore()
                qp.drawText(QPointF(x_adv + 6, oy + 13), f"{adv:.2f}")


    def _draw_stroke(self, qp, stroke, ox, oy, cs):
        if len(stroke) < 2: return
        for i in range(len(stroke)-1):
            x1, y1, p1 = stroke[i]
            x2, y2, p2 = stroke[i+1]
            # 与导出共用同一个宽度公式, 只做 em -> 像素的换算, 保证预览=成品
            w = stroke_width_em((p1+p2)/2, self.stroke_width_max, UNITS_PER_EM) * cs / UNITS_PER_EM
            pen = QPen(QColor(30, 30, 30))
            pen.setWidthF(w)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            qp.setPen(pen)
            qp.drawLine(QPointF(ox+x1*cs, oy+y1*cs), QPointF(ox+x2*cs, oy+y2*cs))


def _smooth_stroke(stroke, win=2):
    """滑动平均去抖 (保留首尾端点), 压感取平均"""
    n = len(stroke)
    if n < 5:
        return stroke
    out = [stroke[0]]
    for i in range(1, n - 1):
        lo, hi = max(0, i - win), min(n, i + win + 1)
        seg = stroke[lo:hi]
        m = len(seg)
        out.append((sum(p[0] for p in seg) / m,
                    sum(p[1] for p in seg) / m,
                    sum(p[2] for p in seg) / m))
    out.append(stroke[-1])
    return out


def _safe_ps_name(name):
    """PostScript 名只能是 ASCII 且不含空格"""
    s = "".join(ch for ch in name if 32 < ord(ch) < 127).strip().replace(" ", "-")
    return s or "HandFont"


def _build_slim_font(tpl, built, built_cps, out_path, font_name, font_version,
                     EM, ASC, DESC, max_pts, max_contours, adv_map=None):
    """精简模式: 从零新建一个只包含手写字形的字体 (无楷体兜底)"""
    hmtx = tpl["hmtx"]
    notdef = TTGlyphPen(None).glyph()
    glyphs = {".notdef": notdef}
    metrics = {".notdef": (EM, 0)}
    order = [".notdef"]
    cmap = {}
    y_min, y_max, x_max = DESC, ASC, EM

    for cp, gname in built_cps:
        if gname in glyphs:          # 同一 glyph 名被多个码点引用
            cmap[cp] = gname
            continue
        g = built[gname]
        glyphs[gname] = g
        order.append(gname)
        # 字宽: 自定义优先; 没设过沿用模板, 保证与"含兜底"版本排版一致;
        # 模板没有的字按全角/半角估
        ch = chr(cp)
        if adv_map and ch in adv_map:
            adv = round(adv_map[ch] * EM)
        elif gname in hmtx.metrics:
            adv = hmtx.metrics[gname][0]
        else:
            adv = EM if cp > 0x2E80 else EM // 2
        lsb = getattr(g, "xMin", 0)
        metrics[gname] = (adv, lsb)
        cmap[cp] = gname
        y_min = min(y_min, getattr(g, "yMin", 0))
        y_max = max(y_max, getattr(g, "yMax", 0))
        x_max = max(x_max, getattr(g, "xMax", EM))

    # 空格类字符没有笔迹可写, 但字体必须给出宽度, 否则打字时空格消失、单词粘连。
    # 宽度取模板真实值(华文楷体: 半角空格 0.25em, 全角空格 1.0em), 模板没有则按 em 估算。
    for cp, gname, fallback in ((0x20, "space", EM // 4), (0x3000, "uni3000", EM)):
        if cp in cmap:
            continue
        adv = hmtx.metrics[gname][0] if gname in hmtx.metrics else fallback
        blank = TTGlyphPen(None).glyph()      # 无轮廓的空字形
        glyphs[gname] = blank
        order.append(gname)
        metrics[gname] = (adv, 0)
        cmap[cp] = gname

    fb = FontBuilder(EM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=ASC, descent=DESC)
    fb.setupNameTable({
        "familyName": font_name,
        "styleName": "Regular",
        "uniqueFontIdentifier": f"{_safe_ps_name(font_name)};{font_version}",
        "fullName": f"{font_name} Regular",
        "psName": f"{_safe_ps_name(font_name)}-Regular",
        "version": f"Version {font_version}",
    })
    # OS/2 的垂直度量必须显式给, 默认全 0 会让 Windows 排版错乱
    fb.setupOS2(
        sTypoAscender=ASC, sTypoDescender=DESC, sTypoLineGap=0,
        # usWin 固定取常量, 不再按当次墨迹外扩: 外扩会让行高随数据浮动(1.00~1.11em),
        # 同一个字体重新导出后 Word 行距就变了。GDI 实测不按这个值裁切墨迹。
        usWinAscent=ASC, usWinDescent=-DESC,
        sxHeight=500, sCapHeight=700,
        fsSelection=0x40,                 # REGULAR
        ulCodePageRange1=(1 << 0) | (1 << 18),   # Latin1 + 简体中文(936)
        usFirstCharIndex=min(cmap), usLastCharIndex=max(cmap),
        achVendID="HFNT", usDefaultChar=0, usBreakChar=32,
    )
    fb.setupPost(keepGlyphNames=False)     # post 3.0, 不存字形名, 更小

    font = fb.font
    if hasattr(font["maxp"], "maxPoints"):
        font["maxp"].maxPoints = max(font["maxp"].maxPoints or 0, max_pts)
        font["maxp"].maxContours = max(font["maxp"].maxContours or 0, max_contours)
    head = font["head"]
    head.xMin, head.yMin, head.xMax, head.yMax = 0, y_min, max(x_max, EM), y_max
    head.macStyle = 0
    font.save(out_path)


def build_ttf(glyph_data, out_path, font_name="HandFont", font_version="1.0",
              progress_cb=None, slim=False, stroke_width=DEFAULT_STROKE_WIDTH,
              outline_cache=None, adv_map=None):
    """生成 TTF。
    slim=False: 以楷体为模板, 保留其 2.5 万字形, 只替换用户写过的字 (体积约 12MB, 有兜底)
    slim=True : 只打包用户写过的字, 从零新建字体 (体积约几百 KB, 未写的字为空白)
    stroke_width: 「笔画粗细」滑块值, 与画布预览用同一公式换算, 保证所见即所得
    outline_cache: 轮廓缓存(ProjectStore), 有则命中时跳过描边+并集(每字 52ms -> 0.6ms);
                   传 None 就是纯现算, 行为和以前完全一样
    adv_map: 自定义拉丁字宽 {字符: em}, 画布上拖字宽虚线得到的值;
             只改 hmtx 的 advance, 不碰字形轮廓, 所以轮廓缓存不受影响
    """

    def report(frac, msg):
        if progress_cb:
            progress_cb(frac, msg)

    report(0.02, "准备模板字体…")
    # 模板字体: 依次尝试常见楷体, 都不存在则报错提示 (候选表见 TEMPLATE_CANDIDATES)
    template = _template_font_path()
    if template is None:
        raise FileNotFoundError("找不到可用的模板字体, 已尝试: " + ", ".join(TEMPLATE_CANDIDATES))
    if slim:
        report(0.08, "正在读取模板字体 (仅取度量参考)…")
        f = TTFont(template)      # 精简模式不必复制模板文件, 直接读原文件取度量即可
    else:
        shutil.copy(template, out_path)
        report(0.08, "正在读取模板字体 (约 2.5 万字，请稍候)…")
        f = TTFont(out_path)
    # 关键提速: 关掉全局包围盒重算, 未改动的 2.5 万字直接复用原始二进制
    # (默认 True 会强制展开全部字形, 保存要十几秒)
    f.recalcBBoxes = False
    EM = f["head"].unitsPerEm  # 模板的 em-square (首选的华文楷体是 1000; 具体值随实际选中的模板而定)
    # 行高度量由常量给定, 不再照抄模板(华文楷体 986/-315 会让行高到 1.30em, 字在行里显小)。
    # 这两个值只用于精简模式; 完整模式沿用模板原值 —— 模板里还有 3083 个汉字低到 -0.315em、
    # 59 个高到 +0.986em, 压到 1.00em 会让这些楷体兜底字压进相邻行(手写字本身在 1.00em 内, 不受影响)。
    ASC = round(ASCENT_EM * EM)
    DESC = round(DESCENT_EM * EM)

    # 通过模板cmap反查每个字符对应的glyph名
    cmap0 = None
    for t in f["cmap"].tables:
        if t.platformID == 3:
            cmap0 = t.cmap
            break
    if cmap0 is None:
        cmap0 = f["cmap"].tables[0].cmap

    # 只替换写过的字的glyph, 其他保留模板原样
    glyf = None if slim else f["glyf"]
    built = {}          # slim 模式: glyph名 -> 新字形
    built_cps = []      # slim 模式: (码点, glyph名) 顺序
    pending_cache = []  # 本次现算出来的轮廓, 导出成功后一次性写回缓存
    n_cached = n_computed = 0   # 统计: 命中多少、现算多少, 供界面提示
    total = max(1, len(glyph_data))
    max_pts = 0
    max_contours = 0
    max_char = ""
    for i, (ch, strokes) in enumerate(glyph_data.items()):
        report(0.15 + 0.70 * i / total, f"正在生成字形 {i+1}/{total}：{ch}")
        cp = ord(ch)
        gname = cmap0.get(cp)
        if slim:
            # 精简模式: 模板里没有的字也照样打包, 用 uniXXXX 命名
            if not gname:
                gname = "uni%04X" % cp
        elif not gname or gname not in glyf.glyphs:
            continue
        # 轮廓来源: 缓存命中就直接取用(里面存的是已算完的最终轮廓), 否则现算后存起来
        # 拉丁字符按四线三格映射(基线在格子 75% 处), 汉字按米字格映射, 两者用不同缓存键
        latin = is_latin_char(ch)
        y_base = (LATIN_BOTTOM_EM if latin else GLYPH_BOTTOM_EM) * EM
        key = _outline_key(stroke_width, EM, latin)
        contours = outline_cache.get_outline(ch, key) if outline_cache is not None else None
        if contours is not None:
            n_cached += 1
        else:
            n_computed += 1
            # 一个字的全部笔画先画进同一条路径, 再做布尔并集(simplified)。
            # 原来 30~50 个互相重叠的封闭轮廓直接写进 TTF: 常规渲染靠 nonzero 填充看不出问题,
            # 但 Word 的"模拟加粗"是对轮廓做几何膨胀, 埋在墨迹内部的重叠边缘一膨胀就自相交,
            # 结果就是"有的笔画加粗了、有的没变、有的只有半截变粗"。并集成互不重叠的轮廓后,
            # 任何一种加粗算法都只有一个外边界可走, 不会再漏。顺带点数 -45%, 文件更小。
            glyph_path = QPainterPath()
            glyph_path.setFillRule(Qt.FillRule.WindingFill)
            for s in strokes:
                if len(s) < 2: continue
                s = _smooth_stroke(s)   # 先去抖, 消除手写抖动造成的毛刺
                n_pts = len(s)
                # 分小段实现压感变宽; 段间重叠2点 + 圆头, 衔接处无豁口
                seg_size = max(3, n_pts // 10)
                for si in range(0, n_pts-1, seg_size):
                    i1 = si
                    i2 = min(si + seg_size + 2, n_pts - 1)
                    if i1 >= i2: continue
                    seg_pts = s[i1:i2+1]
                    avg_p = sum(pt[2] for pt in seg_pts) / len(seg_pts)
                    # 与画布预览共用同一公式(只把 em 换算成模板单位), 调滑块粗细所见即所得
                    width = stroke_width_em(avg_p, stroke_width, EM)
                    path = QPainterPath()
                    path.moveTo(seg_pts[0][0]*EM, (1.0-seg_pts[0][1])*EM + y_base)
                    for pt in seg_pts[1:]:
                        path.lineTo(pt[0]*EM, (1.0-pt[1])*EM + y_base)
                    stroker = QPainterPathStroker()
                    stroker.setWidth(width)
                    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
                    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    glyph_path.addPath(stroker.createStroke(path))
            # 并集, 再丢弃零面积的退化轮廓(simplified 会留下若干面积恰为 0 的碎屑)
            merged = glyph_path.simplified()
            contours = []
            for poly in merged.toSubpathPolygons():
                n = poly.count()
                if n < 3: continue
                area = 0.0
                for k in range(n):
                    a = poly[k]; b = poly[(k+1) % n]
                    area += (b.x() - a.x()) * (b.y() + a.y())
                if abs(area) / 2.0 < UNION_MIN_AREA:
                    continue
                # 先抽稀再写笔: 圆头圆弧和直线段上的密集点对视觉无贡献, 删掉可砍 2/3 体积
                pts = _simplify_poly(
                    [(poly[k].x(), poly[k].y()) for k in range(n)], SIMPLIFY_EPS)
                if len(pts) < 3:
                    continue
                # 存整数: TTF 坐标本身就是整数, 这样缓存取回来和现算的结果一模一样
                contours.append([(int(round(x)), int(round(y))) for x, y in pts])
            if outline_cache is not None:
                pending_cache.append((ch, key, contours))
        pen = TTGlyphPen(None)
        for c in contours:
            pen.moveTo(c[0])
            for q in c[1:]:
                pen.lineTo(q)
            pen.closePath()
        g = pen.glyph()
        g.recalcBounds(glyf)   # 手工重算这一个字的包围盒 (精简模式下 glyf 为 None 同样有效)
        if slim:
            built[gname] = g
            built_cps.append((cp, gname))
        else:
            glyf.glyphs[gname] = g
        # 手写轮廓点数远超模板原字形, 必须同步抬高 maxp 上限,
        # 否则 Windows(GDI/DirectWrite) 会因超限直接不渲染, 字体查看器全空白。
        # 每次导出按当次实际数据重算, 新增字/重写字都会自动跟上。
        n_coords = len(g.coordinates)
        if n_coords > max_pts:
            max_pts = n_coords
            max_char = ch
        if g.numberOfContours > max_contours:
            max_contours = g.numberOfContours

    # maxp 这两项是 uint16, 硬上限 65535, 超限会写出损坏字体, 提前报错 (两种模式都适用)
    if max_pts > 65535 or max_contours > 65535:
        try:
            os.remove(out_path)   # 清掉可能已复制出来的模板副本
        except OSError:
            pass
        raise ValueError(
            f"字形「{max_char}」的轮廓点数达到 {max_pts}，超过 TTF 规范上限 65535"
            "（maxp 字段为 uint16）。请把这个字重画一遍，笔画写快一些以减少采样点。")

    # 全部字形都算完才写缓存: 中途取消或失败就什么都不存, 不会留下半套数据
    if outline_cache is not None and pending_cache:
        report(0.86, f"正在保存轮廓缓存（{len(pending_cache)} 字）…")
        try:
            outline_cache.put_outlines(pending_cache)
        except Exception as e:
            print("轮廓缓存写入失败(不影响本次导出):", e)   # 缓存是附属数据, 坏了下次重算即可

    stats = {"cached": n_cached, "computed": n_computed}
    if slim:
        report(0.88, "正在打包精简字体…")
        _build_slim_font(f, built, built_cps, out_path, font_name, font_version,
                         EM, ASC, DESC, max_pts, max_contours, adv_map)
        report(1.0, "导出完成")
        return stats

    # 完整模式: 自定义字宽逐字覆写 hmtx(只动 advance, 左边距保持模板原值)
    if adv_map:
        hmtx_full = f["hmtx"]
        for ch, adv in adv_map.items():
            gname = cmap0.get(ord(ch))
            if gname and gname in hmtx_full.metrics:
                old = hmtx_full.metrics[gname]
                hmtx_full.metrics[gname] = (round(adv * EM), old[1])

    maxp = f["maxp"]
    if hasattr(maxp, "maxPoints"):
        maxp.maxPoints = max(maxp.maxPoints, max_pts)
        maxp.maxContours = max(maxp.maxContours, max_contours)

    # 名称: 修改现有记录
    name_map = {1: font_name, 2: "Regular", 4: font_name+" Regular",
                5: "Version "+font_version,
                6: font_name+"-Regular", 16: font_name, 17: "Regular"}
    for rec in f["name"].names:
        if rec.nameID in name_map:
            rec.string = name_map[rec.nameID]

    report(0.88, "正在写入 TTF 文件…")
    f.save(out_path)
    report(1.0, "导出完成")
    return stats


class ElidedLabel(QLabel):
    """单行标签: 放不下时按右侧省略号截断。
    普通 QLabel 会把完整文本的宽度当成布局的最小宽度, 文字一变长就把窗口顶大,
    这里让它在空间不足时自动截断, 不再影响布局尺寸。"""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        # 宽度 Ignored: 布局给多少就用多少, 不用文本長度参与最小宽度计算
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self):
        return QSize(0, super().sizeHint().height())

    def paintEvent(self, event):
        fm = self.fontMetrics()
        elided = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        qp = QPainter(self)
        qp.drawText(self.rect(), int(self.alignment()), elided)


def big_button(text):
    btn = QPushButton(text)
    btn.setMinimumHeight(44)
    btn.setFont(QFont("Microsoft YaHei", 10))
    # 收窄左右内边距: 默认边距让两字按钮宽 81px, 七连排会把窗口最小宽度顶到 936
    btn.setStyleSheet("QPushButton { padding-left: 6px; padding-right: 6px; }")
    return btn


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HandFont - 手写字体生成器")
        self.resize(900, 600)
        self.saved_data = {}
        self.adv_data = {}         # 自定义拉丁字宽 {字符: em}(画布拖虚线得到)
        self._dirty = set()          # 已改动但还没落盘的字 (方向键微调用)
        self._load_project()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget(); lout = QVBoxLayout(left)
        lout.setContentsMargins(4, 4, 4, 4)
        lout.setSpacing(4)
        left.setMinimumWidth(230)     # 再窄 10 列格子(每格最小 16px)就排不下了
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索字符…")
        self.search.setMinimumHeight(30)
        self.search.textChanged.connect(self.filter_list)
        lout.addWidget(self.search)

        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("排序:"))
        self.sort_combo = QComboBox()
        # 不要按最长条目计算宽度, 否则长文本"编码顺序（GB2312 区位）"会把左栏顶宽,
        # 导致整个窗口缩不下去; 按最短条目定宽, 窄窗口下长条目由下拉框自己截断
        self.sort_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sort_combo.setMinimumContentsLength(4)
        self.sort_combo.addItems(["编码顺序（GB2312 区位）", "字频优先（常用字在前）"])
        self.sort_combo.setToolTip(
            "编码顺序：完全按 GB2312 区位表排列，便于与标准表逐格对照。\n"
            "字频优先：符号仍按编码顺序，汉字按使用频率排列——\n"
            "　　　　　先写高频字，写到 1000 字即可覆盖日常文本约 92%。")
        self.sort_combo.currentIndexChanged.connect(self.on_sort_changed)
        # 默认「字频优先」: 下拉框先摆好位置再连线, 避免初始化时白重建一遍表格
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(1 if DEFAULT_SORT_BY_FREQ else 0)
        self.sort_combo.blockSignals(False)
        sort_row.addWidget(self.sort_combo, stretch=1)
        lout.addLayout(sort_row)

        # 两张字符卡片做成选项卡(属性页样式): 单击左上角的卡片名切换,
        # 一次只显示一张, 不再上下堆叠抢走半屏高度
        self.card_gb = CharCard()
        self.card_ascii = CharCard()
        self.list_gb = self.card_gb.view
        self.list_ascii = self.card_ascii.view
        # 格子里已写的字画黑色、未写的画灰色: 数据源就是工程字典本身
        for v in (self.list_gb, self.list_ascii):
            v.set_written_source(self.saved_data)
        self._fill_gb(by_freq=DEFAULT_SORT_BY_FREQ)
        for ch in ASCII_CHARS:
            self._add_item(self.list_ascii, ch)
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setFont(QFont("Microsoft YaHei", 9))
        self.tabs.addTab(self.card_gb, "GB2312 字符集")
        self.tabs.addTab(self.card_ascii, "半角 ASCII")
        self.tabs.setTabToolTip(0, "GB2312 全部 7445 个字符：区1-9 符号 + 区16-55 一级汉字 + 区56-87 二级汉字")
        self.tabs.setTabToolTip(1, "半角 ASCII 共 94 个（0x21-0x7E），不属于 GB2312 字符集，作附录单独一页")
        lout.addWidget(self.tabs, stretch=1)
        for v in (self.list_gb, self.list_ascii):
            v.currentRowChanged.connect(
                lambda row, view=v: self.on_char_changed(view, row))
        self.tabs.currentChanged.connect(self.on_tab_changed)
        right = QWidget(); rout = QVBoxLayout(right)
        rout.setContentsMargins(4, 4, 4, 4)
        self.canvas = GlyphCanvas()
        rout.addWidget(self.canvas, stretch=1)
        self.canvas.nudgeRequested.connect(self.nudge_active)
        self.canvas.resetRequested.connect(self.reset_active_offset)
        self.canvas.statusChanged.connect(self._refresh_status)
        self.canvas.set_adv_source(self.adv_data)
        self.canvas.advEdited.connect(self._adv_edited)
        # 连续按方向键时避免每次都写盘, 停手 0.5 秒后再存
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._flush_dirty)
        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("笔画粗细:"))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        # 范围 2-100: 2 已接近"细到极限"(再小受公式里 1.5 的常数项限制, 看不出差别);
        # 100 是全宽 0.36 em 的粗黑体级别, 再往上只会糊成一团黑块
        self.slider.setRange(2, 100); self.slider.setValue(DEFAULT_STROKE_WIDTH)
        self.slider.setMaximumWidth(180)
        self.slider.valueChanged.connect(self.canvas.set_stroke_width)
        self.slider_label = QLabel(str(DEFAULT_STROKE_WIDTH))
        self.slider.valueChanged.connect(lambda v: self.slider_label.setText(str(v)))
        slider_row.addWidget(self.slider); slider_row.addWidget(self.slider_label)
        self.ghost_cb = QCheckBox("淡显底字")
        self.ghost_cb.setChecked(True)
        self.ghost_cb.setToolTip("在格子里用淡色印出当前要写的字，照着笔画描——\n"
                                 "底字只作参考，不会写进导出的字体里。")
        self.ghost_cb.toggled.connect(self.canvas.set_show_ghost)
        slider_row.addWidget(self.ghost_cb)
        slider_row.addStretch()
        rout.addLayout(slider_row)
        # 字体名称/版本
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("字体名:"))
        self.font_name_edit = QLineEdit("HandFont")
        self.font_name_edit.setMaximumWidth(150)
        name_row.addWidget(self.font_name_edit)
        name_row.addWidget(QLabel("版本:"))
        self.font_ver_edit = QLineEdit("1.0")
        self.font_ver_edit.setMaximumWidth(60)
        name_row.addWidget(self.font_ver_edit)
        name_row.addStretch()
        rout.addLayout(name_row)
        btn_row = QHBoxLayout()
        self.btn_undo = big_button("撤销"); self.btn_undo.clicked.connect(self.canvas.undo_stroke)
        self.btn_clear = big_button("清除"); self.btn_clear.clicked.connect(self.canvas.clear_active)
        self.btn_clear_all = big_button("全清"); self.btn_clear_all.clicked.connect(self.canvas.clear_all)
        self.btn_prev = big_button("上一个"); self.btn_prev.clicked.connect(lambda: self._move(-1))
        self.btn_save = big_button("保存"); self.btn_save.clicked.connect(self.save_current)
        self.btn_save.setToolTip("保存当前格（Ctrl+S）。\n"
                                 "当前格是空的、而这个字已经写过时，保存会把它恢复为未写状态。")
        self.btn_next = big_button("下一个"); self.btn_next.clicked.connect(lambda: self._move(1))
        self.btn_export = big_button("导出TTF"); self.btn_export.clicked.connect(self.export_ttf)
        for b in [self.btn_undo, self.btn_clear, self.btn_clear_all,
                  self.btn_prev, self.btn_save, self.btn_next, self.btn_export]:
            btn_row.addWidget(b)
        rout.addLayout(btn_row)
        # 底部状态栏: 字数 + 当前字/微调提示, 空间不足时自动截断, 不撑大窗口
        self.info_label = ElidedLabel("已保存: 0 字")
        self.info_label.setFont(QFont("Microsoft YaHei", 9))
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.info_label.setMinimumHeight(self.info_label.fontMetrics().height() + 2)
        rout.addWidget(self.info_label)
        splitter.addWidget(left); splitter.addWidget(right)
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 700])
        self.setCentralWidget(splitter)
        # 卡片填充发生在画布创建之前, 这里把当前选中字补送到画布
        self.on_char_changed(self.list_gb, self.list_gb.currentRow())
        self._refresh_info()

    # ---------- 左侧两张字符卡片(选项卡切换) ----------
    def _add_item(self, view, ch):
        item = QListWidgetItem(ch)          # 只显示字符本身
        item.setData(Qt.ItemDataRole.UserRole, ch)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        pos = gb2312_position(ch)
        if pos:
            # 码位与区位都放悬停提示, 不占格子; 便于和 GB2312 区位表逐格对照
            item.setToolTip(f"{ch}   U+{ord(ch):04X}   GB2312 区{pos[0]:02d} 位{pos[1]:02d}")
        else:
            item.setToolTip(f"{ch}   U+{ord(ch):04X}   半角 ASCII（不属于 GB2312 字符集）")
        view.addItem(item)

    def _fill_gb(self, by_freq):
        """重建 GB2312 卡片的内容; by_freq=True 时汉字按使用频率排列"""
        v = self.list_gb
        keep = None
        cur = v.currentItem()
        if cur is not None:
            keep = cur.data(Qt.ItemDataRole.UserRole)
        chars = gb2312_by_freq() if by_freq else GB2312_CHARS
        v.blockSignals(True)                 # 重建期间别触发选字回调
        v.setUpdatesEnabled(False)
        try:
            v.clear()
            for ch in chars:
                self._add_item(v, ch)
        finally:
            v.setUpdatesEnabled(True)
            v.blockSignals(False)
        # 默认落在第一个汉字上 (全角空格等符号格没法"写", 不适合做初始位)
        row = chars.index(keep) if keep in chars else GB2312_SYMBOL_COUNT
        v.setCurrentRow(row)
        v.sync_grid()
        self.filter_list(self.search.text())  # 重建后重新套用当前搜索词

    def on_sort_changed(self, idx):
        self._fill_gb(by_freq=(idx == 1))

    def _active_view(self):
        """当前所显示的那张卡片的表格 (选项卡切换后可见的那张)"""
        return self.list_ascii if self.tabs.currentIndex() == 1 else self.list_gb

    @staticmethod
    def _first_visible_row(view):
        """第一个没被搜索过滤掉的格子, 找不到返回 -1"""
        for i in range(view.count()):
            it = view.item(i)
            if it is not None and not it.isHidden():
                return i
        return -1

    def on_tab_changed(self, idx):
        """切到另一张卡片时, 把画布换到那张卡片当前选中的字符上"""
        view = self.list_ascii if idx == 1 else self.list_gb
        row = view.currentRow()
        if row < 0:
            # 这张卡片还没选过字: 落在第一个可见格上 (setCurrentRow 会触发 on_char_changed)
            row = self._first_visible_row(view)
            if row < 0:
                return
            view.setCurrentRow(row)
        else:
            self.on_char_changed(view, row)
        # 隐藏时算不出列宽, 显示后再排一次 10 列网格
        QTimer.singleShot(0, view.sync_grid)

    def filter_list(self, text):
        text = text.strip()
        for v in (self.list_gb, self.list_ascii):
            v.setUpdatesEnabled(False)       # 七千多项逐个显隐, 关掉重绘避免卡顿
            try:
                for i in range(v.count()):
                    item = v.item(i)
                    ch = item.data(Qt.ItemDataRole.UserRole)
                    item.setHidden(bool(text) and text not in ch)
            finally:
                v.setUpdatesEnabled(True)
        if not hasattr(self, "canvas") or not text:
            return                            # 初始化阶段还没有画布; 清空搜索时保持原位
        # 当前选中的格子可能被过滤掉了, 表格里会显得"一个都没选中";
        # 挪到第一个命中的格子, 本页没有命中就跳到另一张卡片
        view = self._active_view()
        cur = view.currentItem()
        if cur is not None and not cur.isHidden():
            return
        row = self._first_visible_row(view)
        if row >= 0:
            view.setCurrentRow(row)
            return
        other = self.list_ascii if view is self.list_gb else self.list_gb
        row = self._first_visible_row(other)
        if row >= 0:
            other.setCurrentRow(row)          # 触发 on_char_changed 换字
            self.tabs.setCurrentIndex(1 if other is self.list_ascii else 0)

    def on_char_changed(self, view, row):
        if row < 0:
            return
        item = view.item(row)
        if item is None:
            return
        ch = item.data(Qt.ItemDataRole.UserRole)
        # 两张卡片各自记住自己的选字位置, 切回来还停在原处;
        # 同时只有一张卡片可见, 所以不会出现两处同时高亮
        self.canvas.set_char(ch, self.saved_data.get(ch))
        self._refresh_info()

    def save_current(self):
        """保存当前格。
        当前格是空的时: 这个字还没写过 → 只提示一句(不弹窗打断);
        已经写过 → 询问是否把该字恢复成"未写"并删掉存档。
        否则写过的字就再也删不掉了。"""
        ch = self.canvas.current_char
        strokes = self.canvas.get_active_strokes()
        if not strokes:
            if ch not in self.saved_data:
                self._flash(f"「{ch}」还没写，这一格不用保存")
                return
            other = [str(i + 1) for i, c in enumerate(self.canvas.cells) if c]
            extra = (f"\n\n注意：第 {'、'.join(other)} 格还有笔迹，但只有当前格会被保存。"
                     if other else "")
            ret = QMessageBox.question(
                self, "恢复为未写",
                f"当前格是空的。保存会删掉「{ch}」已保存的笔画，这个字回到未写状态"
                f"（导出时不再包含它）。{extra}\n\n要继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret == QMessageBox.StandardButton.Yes:
                self._delete_char(ch)
            return
        self.saved_data[ch] = [list(s) for s in strokes]
        self._save_char(ch)
        self._repaint_lists()      # 这个字由灰变黑
        self._refresh_info()
        # 自动跳下一个
        self._move(1)

    def _delete_char(self, ch):
        """把一个字恢复成未写状态: 内存、数据库、列表颜色、统计一起更新。
        自定义字宽也一并清掉 —— "恢复为未写"就是回到完全的默认状态。"""
        self.saved_data.pop(ch, None)
        self.adv_data.pop(ch, None)
        self._dirty.discard(ch)     # 待补存队列里也要撤掉, 否则又会被写回去
        try:
            self.store.delete(ch)
        except Exception as e:
            QMessageBox.warning(self, "删除失败", str(e))
            return
        self._repaint_lists()
        self._refresh_info()
        self._flash(f"已把「{ch}」恢复为未写")

    def _flash(self, msg):
        """状态栏临时提示一句(不弹窗); 下次状态刷新会自然覆盖"""
        self.info_label.setText(f"{msg}　　{self.canvas.hint_text()}")

    def _load_project(self):
        """打开工程库; 首次运行(或旧版 JSON 还在)时自动迁移一次"""
        self.store = ProjectStore(PROJECT_DB)
        if self.store.count() == 0 and os.path.exists(PROJECT_FILE):
            self._migrate_from_json()
        self.saved_data.update(self.store.load())
        self.adv_data.update(self.store.load_adv())

    def _migrate_from_json(self):
        """旧版 handfont_project.json → 新的单字存储。
        成功后把 JSON 改名成 .json.bak 留在原处当备份, 不删用户数据。"""
        try:
            with open(PROJECT_FILE, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception as e:
            print("读取旧工程失败, 跳过迁移:", e)
            return
        items = [(ch, [[tuple(pt) for pt in s] for s in v]) for ch, v in data.items()]
        dlg = QProgressDialog("正在把旧工程迁移到新格式（只做一次）…", None, 0, len(items), self)
        dlg.setWindowTitle("迁移工程")
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setCancelButton(None)
        dlg.show()

        def report(done, total):
            dlg.setValue(done)
            QApplication.processEvents()

        try:
            self.store.put_many(items, report)
        except Exception as e:
            dlg.close()
            QMessageBox.warning(self, "迁移失败",
                                f"旧工程导入失败，原 handfont_project.json 未改动。\n{e}")
            return
        dlg.close()
        try:
            os.replace(PROJECT_FILE, PROJECT_FILE + ".bak")   # 原子改名, 不会丢文件
        except OSError as e:
            print("旧 JSON 改名失败(不影响使用):", e)

    def _save_char(self, ch):
        """只写这一个字, 与总字数无关; 字宽随笔画一起落库(重写不丢)"""
        try:
            self.store.put(ch, self.saved_data[ch], self.adv_data.get(ch))
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def _adv_edited(self, ch):
        """画布上的字宽虚线拖完了/双击复位了: 立刻落库(该字写过的前提下)。
        还没写过的字没地方存, 先留在内存里, 写完保存时随笔画一起写进库。"""
        try:
            self.store.set_adv(ch, self.adv_data.get(ch))
        except Exception as e:
            print("字宽保存失败(下次保存笔画时会再试):", e)

    def _flush_dirty(self):
        """方向键微调停下 0.5 秒后的补存: 只写被挪动过的那几个字"""
        if not self._dirty:
            return
        for ch in list(self._dirty):
            if ch in self.saved_data:
                self._save_char(ch)
        self._dirty.clear()

    def closeEvent(self, event):
        self._flush_dirty()
        self.store.close()
        super().closeEvent(event)

    def _refresh_status(self):
        """底部状态栏: 已写字数 + 当前字/微调提示(提示原在画布顶部, 会挡住米字格)"""
        self.info_label.setText(
            f"已保存: {len(self.saved_data)} 字　　{self.canvas.hint_text()}")

    def _repaint_lists(self):
        """保存状态变了, 让两张卡片的格子重画一遍 (格子黑白取决于 saved_data)"""
        for v in (self.list_gb, self.list_ascii):
            v.viewport().update()

    def _refresh_info(self):
        self._refresh_status()
        # GB2312 卡片: 分区进度, 一眼看出还差多少
        cov = gb2312_coverage(self.saved_data)
        done = sum(v[0] for v in cov.values())
        total = sum(v[1] for v in cov.values())
        sym_done = sum(v[0] for n, v in cov.items() if n.startswith("区"))
        sym_total = sum(v[1] for n, v in cov.items() if n.startswith("区"))
        l1 = cov["一级汉字"]; l2 = cov["二级汉字"]
        self.card_gb.prog.setText(
            f"已写 {done}/{total}（{done * 100.0 / total:.1f}%）\n"
            f"符号 {sym_done}/{sym_total}　一级 {l1[0]}/{l1[1]}　二级 {l2[0]}/{l2[1]}")
        # 半角 ASCII 卡片: 进度 + 还缺哪几个
        n_ascii = sum(1 for c in self.saved_data if ord(c) < 128)
        miss = [c for c in ASCII_CHARS if c not in self.saved_data]
        tail = f"　缺 {len(miss)} 个：{''.join(miss)}" if miss else "　已写全"
        self.card_ascii.prog.setText(f"已写 {n_ascii}/94{tail}")

    def _move(self, delta):
        view = self._active_view()
        row = view.currentRow()
        if 0 <= row+delta < view.count():
            view.setCurrentRow(row+delta)

    def _shift_saved(self, ch, dx, dy):
        """把已存档的笔画同步平移, 否则重新导出的字体里看不到这次微调。
        只有第 1 格(存档格)会落到库里, 其余格子写完不保存就只是草稿。"""
        if self.canvas.active_cell != 0 or ch not in self.saved_data:
            return
        self.saved_data[ch] = [
            [(p[0] + dx, p[1] + dy, p[2]) for p in s]
            for s in self.saved_data[ch]
        ]
        self._dirty.add(ch)          # 停手 0.5 秒后只补存这一个字
        self._save_timer.start()

    def nudge_active(self, dx, dy):
        applied = self.canvas.nudge_active_cell(dx, dy)
        if applied is None:
            return False
        self._shift_saved(self.canvas.current_char, *applied)
        return True

    def reset_active_offset(self):
        applied = self.canvas.reset_active_offset()
        if applied is None:
            return False
        self._shift_saved(self.canvas.current_char, *applied)
        return True

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.save_current()
        else:
            # 方向键由画布自己处理(需要画布有焦点); 这里不再重复处理,
            # 免得在左侧列表里按方向键时误挪字符
            super().keyPressEvent(event)

    def export_ttf(self):
        if not self.saved_data:
            QMessageBox.warning(self, "提示", "还没有保存任何字")
            return
        dlg = QFileDialog(self, "导出 TTF", "HandFont.ttf", "TTF 字体 (*.ttf)")
        dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dlg.setDefaultSuffix("ttf")
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)   # 才能塞进勾选框
        # Qt 官方的 qtbase_zh_CN 翻译漏了这两条, 自绘对话框里会露出英文, 手工补上
        dlg.setLabelText(QFileDialog.DialogLabel.LookIn, "查找范围(&I)：")
        dlg.setLabelText(QFileDialog.DialogLabel.FileType, "文件类型(&T)：")
        slim_cb = QCheckBox("精简模式（仅包含写过的字）")
        slim_cb.setChecked(DEFAULT_SLIM_EXPORT)   # 默认勾选: 只导出已写的字
        slim_cb.setToolTip(
            "勾选：只打包你写过的字，体积约几百 KB，缩小约 40 倍。\n"
            "　　　没写过的字在文档里会显示为空白缺字。\n"
            "不勾选：保留楷体模板的全部字形做兜底，体积约 12 MB。")
        dlg.layout().addWidget(slim_cb)
        if not _qt_exec(dlg):
            return
        files = dlg.selectedFiles()
        if not files:
            return
        self._export_to(files[0], slim_cb.isChecked())

    def _export_to(self, path, slim):
        total = len(self.saved_data)
        dlg = QProgressDialog("准备导出…", "取消", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setWindowTitle("导出 TTF")
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        dlg.show()

        class _Cancelled(Exception):
            pass

        def cb(frac, msg):
            QApplication.processEvents()
            if dlg.wasCanceled():
                raise _Cancelled()
            dlg.setValue(int(frac * 100))
            dlg.setLabelText(msg)
            QApplication.processEvents()

        stats = None
        try:
            stats = build_ttf(self.saved_data, path,
                              self.font_name_edit.text().strip() or 'HandFont',
                              self.font_ver_edit.text().strip() or '1.0',
                              progress_cb=cb, slim=slim,
                              stroke_width=self.canvas.stroke_width_max,
                              outline_cache=self.store,
                              adv_map={ch: v for ch, v in self.adv_data.items()
                                       if is_latin_char(ch)})
        except _Cancelled:
            dlg.close()
            try:
                if os.path.exists(path):
                    os.remove(path)   # 清掉半截文件
            except Exception:
                pass
            QMessageBox.information(self, "已取消", "导出已取消，未生成字体文件。")
            return
        except Exception as e:
            dlg.close()
            # 导出中途失败时 out_path 里只剩模板副本, 删掉避免误以为导出成功
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            QMessageBox.critical(self, "导出失败", str(e))
            return
        dlg.setValue(100)
        dlg.close()
        try:
            mb = os.path.getsize(path) / 1048576.0
            size_txt = f"{mb:.2f} MB" if mb >= 1 else f"{os.path.getsize(path)/1024:.0f} KB"
        except OSError:
            size_txt = "?"
        mode_txt = "精简模式，仅含写过的字" if slim else "含楷体兜底字形"
        # 第一次(或改过粗细之后)要把所有轮廓现算一遍, 之后直接从缓存取, 快几十倍
        hint = ""
        if stats and stats["computed"] >= 200:
            hint = (f"\n\n本次为 {stats['computed']} 个字计算了轮廓并已缓存，"
                    f"字形和粗细不变的话下次导出只需几秒。")
        QMessageBox.information(self, "完成",
            f"已导出 {total} 个字符（{mode_txt}）\n文件大小: {size_txt}\n{path}{hint}")


def _qt_exec(obj):
    """Qt6 是 exec(), Qt5(PySide2) 是 exec_()。PySide6 里 exec_ 只是弃用别名,
    所以优先用 exec, 没有再退回 exec_。"""
    if hasattr(obj, "exec"):
        return obj.exec()
    return obj.exec_()


def _qt_translations_path():
    """Qt 内置翻译目录: Qt6 是 QLibraryInfo.path(...), Qt5 是 QLibraryInfo.location(...)"""
    if hasattr(QLibraryInfo, "path") and hasattr(QLibraryInfo, "LibraryPath"):
        return QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    return QLibraryInfo.location(QLibraryInfo.TranslationsPath)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    # 窗口/任务栏图标: app.ico 是唯一的图标源, 打包时会被打进资源目录, 源码运行时读旁边的
    _p = os.path.join(_res_dir(), "app.ico")
    if os.path.exists(_p):
        app.setWindowIcon(QIcon(_p))
    # Qt 自带对话框(比如关掉原生样式后自绘的"导出 TTF"保存框)的内置文字
    # ——Save / Cancel / File name 等——默认只有英文, 必须显式装上 Qt 的中文翻译才显示中文
    _tr = QTranslator(app)
    _tr_dir = _qt_translations_path()
    if _tr.load("qtbase_zh_CN", _tr_dir):
        app.installTranslator(_tr)
    win = MainWindow()
    win.show()
    sys.exit(_qt_exec(app))