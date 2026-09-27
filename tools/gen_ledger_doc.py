# -*- coding: utf-8 -*-
"""从 tools/hacs_layers.json + 实测数据生成《HACS 分层台账》单文件 HTML。

为什么用脚本生成：台账是长期参考件，表格必须与机器可读的层归属表同源，
人肉拷贝必然漂移。改层归属 → 改 json → 重跑本脚本 → 文档同步。
"""
import io, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, 'HACS-分层台账.html')

M = json.load(io.open(os.path.join(HERE, 'hacs_layers.json'), encoding='utf-8'))
FILES = M['files']
LAYERS = M['layers']
PORTS = M['ports']

LAYER_TITLES = [
    ('C',  'C 组合根（进程入口装配）'),
    ('L4', 'L4 呈现层 Experience'),
    ('L3', 'L3 协作域 Domain'),
    ('L2', 'L2 适配层 Adapter'),
    ('L1', 'L1 通行层 Transport'),
    ('L0', 'L0 平台层 Host'),
    ('X',  'X 横切（身份与授权）'),
    ('T',  'T 工具面（不属运行路径）'),
]


def lines_of(rel):
    p = os.path.join(ROOT, rel.replace('/', os.sep))
    if not os.path.isfile(p):
        return None
    with io.open(p, 'rb') as fh:
        return fh.read().count(b'\n') + 1


def deps_of(rel, all_mods):
    p = os.path.join(ROOT, rel.replace('/', os.sep))
    if not os.path.isfile(p):
        return []
    src = io.open(p, encoding='utf-8', errors='replace').read()
    me = os.path.splitext(os.path.basename(rel))[0]
    found = re.findall(r'^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)', src, re.M)
    return sorted({x for x in found if x in all_mods and x != me})


ALL_MODS = {os.path.splitext(k.split('/')[-1])[0] for k in FILES}


def esc(s):
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


# ---------- 第二章：按层分组的件清单 ----------
def layer_table(layer):
    rows, tot = [], 0
    for rel, meta in sorted(FILES.items()):
        if meta['layer'] != layer:
            continue
        ln = lines_of(rel) or 0
        tot += ln
        name = rel.replace('agent-team/', '')
        port = meta.get('port')
        deps = deps_of(rel, ALL_MODS)
        note = esc(meta['note'])
        rows.append(
            '<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                esc(name), ln, ('<b>%s</b>' % port) if port else '—',
                esc(', '.join(deps)) or '—', note))
    head = ('<table><thead><tr><th>件</th><th>行数</th><th>端口</th>'
            '<th>层内/跨层依赖</th><th>一句话职责 · 台账备注</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table>')
    return len(rows), tot, head


# ---------- 第四章：实测数字（check_layers 基线）----------
BASELINE = {
    # 前两项由 check_layers.py 实时打印（'分层门禁 · 台账（件数 N，层间 import 边 M）'）——
    # 动结构后必须同步这两个数；下面其余各项是**刀 3 时点**的实测，不是随生成重算。
    # 刀27（阶段5 第三刀）：73 → 77（新增 L0 member_roster.py + T 推导器/装置/旧侧 3 件）、
    # 边 84 → 91。**含一条继承订正**：刀26 登记 gen_nats_conf_equiv.py 时件数已 72 → 73 而本常量漏同步。
    # 刀28（阶段5 第四刀）：77 → 78（新增 _old/membership_audit_old_k28.py——刀28 前正件的逐字节快照，
    # 当装置 D 段的「改前」锚）、边 91 → 94（新件 import L0 node_profile + 懒 import L1 gen_nats_conf
    # + 装置 import 该快照）。
    'files': 78, 'edges': 94, 'direction': 0, 'bizwords': 48,
    'hardcode_files': 25, 'storage': 11, 'storage_over': 2,
    # 刀 3 后：宿主字面量拆成「真硬编码（路径）」与「提示级（IP/端口默认值）」两个口径。
    'host_path_files': 42, 'host_path_sites_before': 111, 'host_path_sites_after': 80,
    'host_path_prod_files_before': 16, 'host_path_prod_sites_before': 27, 'host_path_prod_sites_after': 0,
    'net_files': 19, 'net_sites': 42,
}
HARDCODE_TOP = [
    ('blackboard_view.py', 'L3', 19, 6, '注释里的隧道/deployment 说明 + 运行目录常量'),
    ('cli_backend.py', 'L2', 8, 11, '各 CLI 的代理地址、node.exe 绝对路径'),
    ('teamd.py', 'L1', 6, 7, '管理口/示例地址、运行目录常量'),
    ('blackboard_svc.py', 'L3', 9, 4, 'DSN 示例、健康口、日志路径'),
    ('chat_svc.py', 'L3', 4, 6, 'DSN 示例、默认绑址、claude_exec 绝对路径'),
    ('ui/serve.py', 'L4', 5, 5, '开发用后端地址、本机目录'),
    ('team.py', 'L1', 3, 4, '默认 NATS 入口、留痕文件路径'),
    ('a2a_svc.py', 'L1', 4, 1, '示例绑定地址'),
    ('task_svc.py', 'L3', 2, 4, '默认绑址、库路径'),
    ('group_watch.py', 'L3', 3, 2, '轮询目标地址'),
]
STORAGE_ROWS = [
    ('a2a_svc.py', 'L1', 'sqlite3', True),
    ('ctx_watermark.py', 'L2', 'sqlite3', True),
    ('blackboard_svc.py', 'L3', 'PostgreSQL', False),
    ('task_svc.py', 'L3', 'sqlite3', False),
    ('task_sys/task_svc.py', 'L3', 'sqlite3', False),
    ('roundtable.py', 'L3', 'PostgreSQL', False),
    ('rt_remind.py', 'L3', 'PostgreSQL', False),
    ('shared_content.py', 'L3', 'PostgreSQL + sqlite3', False),
    ('backfill_embeddings.py', 'L3', 'PostgreSQL', False),
    ('session_ctx_archiver.py', 'L3', 'sqlite3', False),
    ('view_auth.py', 'X', 'PostgreSQL + sqlite3', False),
]
PORT_ROWS = [(k, v) for k, v in PORTS.items() if not k.startswith('_')]

BIG = [('chat_svc.py', 'L3', 4588), ('blackboard_svc.py', 'L3', 4362),
       ('blackboard_view.py', 'L3', 3824), ('roundtable.py', 'L3', 2425),
       ('teamd.py', 'L1', 1504), ('shared_content.py', 'L3', 1431),
       ('view_auth.py', 'X', 1396), ('team.py', 'L1', 1040),
       ('task_svc.py', 'L3', 1016), ('ledger_patrol.py', 'T', 944),
       ('a2a_svc.py', 'L1', 894), ('cli_backend.py', 'L2', 869)]

# ---------- 组装 ----------
secs = []
secs.append("""<h2>一、这份台账怎么用</h2>
<div class="callout">
  <b>本文件是《HACS 分层规划》的阶段 0 产出，是层归属与门禁基线的唯一权威版本。</b>
  规划文档（<code>docs/HACS-分层规划-handoff.html</code>）回答「该怎么分、为什么这么分」；本台账回答
  「<b>现在每个件实际站在哪一层、依赖谁、踩了哪条判据、怎么复查</b>」。动结构之前先对台账，
  偏离先升版本再动码。
</div>
<h3>1.1 三个口径</h3>
<ul>
  <li><b>层归属是人判的，不是推的。</b>判断法则沿用规划文档：<i>通信层四节点对等、业务层中枢独有</i>。
      一个件若同时干两层的活，台账里记两个层并打 <span class="badge b-warn">拆分点</span>，
      <b>不为它硬凑一个层</b>。</li>
  <li><b>数字全部来自实测</b>，可复跑：件数/行数 来自 <code>wc -l</code>；门禁五项来自
      <code>python tools/check_layers.py</code>；进程与启动方式来自本机进程与计划任务实查。
      标注 <span class="badge b-plan">会漂移</span> 的，落地前复核。</li>
  <li><b>提示级 vs 硬级。</b>只有「依赖方向」是硬门禁（退出码 1）。业务词、宿主硬编码、存储句柄
      是提示级：它们的命中<b>必须人判</b>（注释里提到「会话」不算越层，真去读别人库算）。
      <b>退出码 2 = 源码树守卫</b>：源文件缺失超过一成时判「查不了」，不给假绿灯
      （对外仓里只有层归属表、没有源码，跑出来会是「件数齐全、零违规」——那是假象）。</li>
</ul>
<h3>1.2 台账的机器可读孪生体</h3>
<p>本文件的所有表格由 <code>tools/gen_ledger_doc.py</code> 从 <code>tools/hacs_layers.json</code>
生成。<b>要改层归属，改 json 再重跑生成脚本</b>，别直接编辑 HTML 表格 —— 否则文档与门禁读的那份口径分家，
门禁就白跑了。</p>""")

# 二、件清单
per_layer = []
total_lines = 0
for code, title in LAYER_TITLES:
    n, ln, table = layer_table(code)
    per_layer.append((code, title, n, ln, table))
    total_lines += ln

body2 = ['<h2>二、件清单总表（按层）</h2>',
         '<p>共登记 <b>%d 个件</b>、<b>%s 行</b>。已排除 <code>tests/</code>、'
         '<code>__pycache__</code> 与归档目录。端口为「全节点同号」的约定值，实际绑定面见各件源码的 '
         '<code>--host</code>。</p>' % (len(FILES), format(total_lines, ','))]
body2.append('<h3>2.1 层规模一览</h3>')
body2.append('<table><thead><tr><th>层</th><th>件数</th><th>行数</th><th>占运行件比重</th>'
             '<th>层定位</th></tr></thead><tbody>')
for code, title, n, ln, _ in per_layer:
    pct = 100.0 * ln / total_lines
    body2.append('<tr><td><b>%s</b></td><td>%d</td><td>%s</td><td>%.1f%%</td><td>%s</td></tr>'
                 % (esc(title), n, format(ln, ','), pct, esc(LAYERS[code])))
body2.append('</tbody></table>')
for i, (code, title, n, ln, table) in enumerate(per_layer):
    body2.append('<h3>2.%d %s（%d 件 / %s 行）</h3>' % (i + 2, esc(title), n, format(ln, ',')))
    body2.append(table)
body2.append('<h3>2.9 台账口径下的「大件」</h3>')
body2.append('<p>规划文档点名的三处最痛耦合，都长在这几只大件里 —— 这一列是拆分的<b>排队依据</b>，'
             '不是美观指标。</p>')
body2.append('<table><thead><tr><th>件</th><th>层</th><th>行数</th><th>为什么它大</th></tr></thead><tbody>')
why = {
    'chat_svc.py': '一个文件同时是网关（静态托管/反代/登录跳转/SSE 聚合）+ 协作域（群/会话/待办）+ 适配细节',
    'blackboard_svc.py': '黑板服务 + PG 层 + 富化 + 检索，全塞一件',
    'blackboard_view.py': '只读视图 + 公网登录门户 + 鉴权，L3 与 L4 同体',
    'roundtable.py': '会议编排 + NATS 推流 + PG 落库',
    'teamd.py': '成员守护 + 多成员身份托管 + 管理口 + 重投语义',
    'shared_content.py': '共享区 + PG + sqlite 双句柄',
    'view_auth.py': '账号/会话/角色/密级/审计的横切实现，被三层共用（正确的大）',
    'team.py': '成员侧 CLI，含点对点/群发/「会议召集」—— 后者是业务语义长在通行层里',
    'task_svc.py': '群任务清单 + 直接持 sqlite',
    'ledger_patrol.py': '台账巡检，规则内嵌',
    'a2a_svc.py': 'A2A 边缘端点（L1）+ 后端分派与上下文自愈（L2），两层的活一件干',
    'cli_backend.py': '各 CLI 的统一后端，代理/路径差异内嵌（L2 该有的样子，但硬编码要收）',
}
for f, l, n in BIG:
    body2.append('<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>'
                 % (esc(f), l, format(n, ','), esc(why.get(f, ''))))
body2.append('</tbody></table>')
secs.append('\n'.join(body2))

# 三、依赖方向
secs.append("""<h2>三、依赖方向实测（门禁检查 1）</h2>
<div class="callout now">
  <b>结论：现有 import 图与五层方向一致 —— 零反向依赖。</b>
  这不代表解耦没问题，而是说明<b>耦合不长在「谁 import 谁」上</b>。真正的越层是三处：
  语义越层（业务词长在通行层）、句柄越层（通行/适配件直读别人的库）、配置静态（改动靠改文件+重启）。
  后两处见第四、七节。
</div>
<h3>3.1 判据与门禁规则</h3>
<ul>
  <li>允许方向：<b>L4 → L3 → L2 → L1 → L0</b>（上层依赖下层）；<b>同层可互依</b>；
      <b>横切 X 可被任意层引用</b>；工具面 T 不受限（不属运行路径）。</li>
  <li><b>组合根 C</b>（进程入口装配）排在方向序列最前：<b>C 可 import 任意层，任何层不得 import C</b>。
      判据逻辑无需特判 —— C 的 rank 最小，<code>rank[src] &lt;= rank[dst]</code> 天然成立/天然违规。
      它的用途是给「反向依赖」一个合法出口：把注入动作放进进程入口，而不是让下层去 import 上层。</li>
  <li>反向依赖（下层 import 上层）= <b>硬违规</b>，退出码 1，必须改。</li>
  <li>工具面缺席会让结果失真：<code>ledger_patrol.py</code> 这类巡检件 import 域件是正常的，
      把它当运行件就会报假违规。</li>
</ul>
<h3>3.2 基线（件数 / 层间 import 边 / 方向违规 —— 由 <code>check_layers.py</code> 实时打印后手抄，非随生成重算）</h3>
<table><thead><tr><th>指标</th><th>实测</th><th>含义</th></tr></thead><tbody>
<tr><td>登记件数</td><td><b>%d</b></td><td>运行件 + 工具件（<b>与 <code>check_layers.py</code> 实时打印的同一数</b>；含 T 面测试件）</td></tr>
<tr><td>层间 import 边</td><td><b>%d</b></td><td>跨层引用（<b>含</b>共用底座件）；同层内引用不计</td></tr>
<tr><td>方向违规</td><td><b>%d</b></td><td>硬门禁今日通过</td></tr>
</tbody></table>
<p class="kpi">层间 import 边从早期基线的 17 涨到 <b>%d</b>，<b>不是「耦合变多」，而是解耦的账单</b>：
把原先各自复制的底座提到唯一出处之后，每个调用点都得 <code>import</code> 一次 ——
L0 <code>node_paths.py</code>（≈19 件引用，按 <code>grep -l</code> 计）、L1 <code>web_gateway.py</code>（5 件）、
横切 <code>view_auth.py</code>（5 件）、L2 <code>agent_usage.py</code>（4 件）四类底座正是边增长的主要来源。
反过来，<b>件与件之间真正的主战场仍不是函数调用</b>：端口、文件、数据库、启动参数这四类通道不写一行
<code>import</code> 也能把两个件焊死，所以解耦必须在<b>契约层</b>收口。数字会漂移，落地前以当次
<code>check_layers.py</code> 的输出为准；第四章的硬编码 / 存储句柄各组数字为<b>刀 3 时点</b>实测，重算要跑各自脚本。</p>""" % (
        BASELINE['files'], BASELINE['edges'], BASELINE['direction'], BASELINE['edges']))

# 四、配置面
h4 = ['<h2>四、配置面实测（门禁检查 3 与 4）</h2>']
h4.append('<h3>4.1 宿主硬编码（判据 4：配置零硬编码）</h3>')
h4.append('<p>原口径（IP 与路径混算）为 <b>%d 件</b>，但混算会把 URL 里的 <code>p:/</code> 误判成盘符、'
          '并把大量<b>合法的</b> loopback / 兜底基址算进来，看不出真实进度。刀 3 已把本项拆成两个口径：'
          '<b>①真硬编码 = 宿主路径字面量</b>（应当单调趋 0，唯一合法去处 = 层归属表声明的 '
          '<code>%s</code>）；<b>②提示级 = IP / 端口默认值</b>（大量属合法默认，只提示不设门禁）。'
          '文档字符串与注释里的示例一律不计。</p>' % (BASELINE['hardcode_files'], 'L0 <code>agent-team/node_paths.py</code>'))
h4.append('<table><thead><tr><th>口径</th><th>刀 3 前</th><th>刀 3 后</th><th>说明</th></tr></thead><tbody>'
          '<tr><td>真硬编码（宿主路径，全仓 .py）</td><td>%d 处</td><td><b>%d 处</b></td>'
          '<td>剩余集中在一批一次性脚本/测试/历史 docs（不跑生产，未纳入本刀）</td></tr>'
          '<tr><td>真硬编码（本次改造的 %d 个生产件）</td><td>%d 处</td><td><b>%d 处</b></td>'
          '<td>全部改走 env &gt; 平台惯例，默认值集中到 L0</td></tr>'
          '<tr><td>提示级（IP/端口默认值）</td><td>%d 件 / %d 处</td><td colspan="2">保持提示级：'
          'loopback 与兜底基址属合法默认，强收反而降低可读性</td></tr></tbody></table>'
          % (BASELINE['host_path_sites_before'], BASELINE['host_path_sites_after'],
             BASELINE['host_path_prod_files_before'], BASELINE['host_path_prod_sites_before'],
             BASELINE['host_path_prod_sites_after'], BASELINE['net_files'], BASELINE['net_sites']))
h4.append('<table><thead><tr><th>件</th><th>层</th><th>IP</th><th>路径</th><th>主要来源</th></tr></thead><tbody>')
for f, l, ip, pa, why_ in HARDCODE_TOP:
    h4.append('<tr><td><code>%s</code></td><td>%s</td><td>%d</td><td>%d</td><td>%s</td></tr>'
              % (esc(f), l, ip, pa, esc(why_)))
h4.append('</tbody></table>')
h4.append('<div class="callout warn"><b>要收的口径：</b>'
          '「<code>os.environ.get(X) or &lt;字面量&gt;</code>」这种写法是<b>可接受的过渡形态</b>'
          '（环境变量优先，字面量只是兜底），但它必须满足两条：'
          '① 兜底值不得是本机专属绝对路径（如 <code>node.exe</code> 的安装路径）；'
          '② 兜底值出现在宿主层（L0）或该件的 profile 里，不出现在 L2/L3 源码中。'
          '现在违反的是第①条：<code>cli_backend.py</code> 里写死了 node 安装路径。</div>')
h4.append('<h3>4.2 直接持存储句柄的件（判据 1 的前置检查）</h3>')
h4.append('<p>规则：<b>只有协作域（L3）与横切身份（X）可以持库句柄</b>；'
          '通行层、适配层、呈现层、平台层只能通过接口拿数据。共 %d 件持句柄，其中 '
          '<b>%d 件越层</b>（下表打 ⚠）：</p>' % (BASELINE['storage'], BASELINE['storage_over']))
h4.append('<table><thead><tr><th></th><th>件</th><th>层</th><th>句柄</th><th>判定</th></tr></thead><tbody>')
for f, l, k, over in STORAGE_ROWS:
    judge = ('<b>越层</b>：所在层不该持句柄' if over else '在本层允许范围')
    h4.append('<tr><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>'
              % ('⚠' if over else '·', esc(f), l, esc(k), judge))
h4.append('</tbody></table>')
h4.append("""<div class="callout warn"><b>这两条越层命中不是巧合 —— 它们正是规划文档里「上下文水位三处口径」
那处最痛耦合的硬证据：</b><code>a2a_svc.py</code>（L1 边缘端点）与 <code>ctx_watermark.py</code>
（L2 适配件）各自用 <code>sqlite3</code> 直读 Agent 的会话库来算上下文占用。同一件事在三处实现、
三处口径，任一处改了另两处不知情。收口方向就是规划文档写的：<b>统一收进 L2 的
<code>/usage</code> 能力</b>，L1 与 L3 一律走接口。</div>
<h3>4.3 层 × 违例矩阵（一眼看哪里最脏）</h3>
<table><thead><tr><th>层</th><th>件数</th><th>方向违规</th><th>L1 业务词</th><th>宿主硬编码件</th><th>越层持句柄</th></tr></thead><tbody>""")
for code, title in LAYER_TITLES:
    cnt = sum(1 for f, m in FILES.items() if m['layer'] == code)
    hc = sum(1 for f, l, *_ in HARDCODE_TOP if l == code)
    ov = sum(1 for f, l, k, o in STORAGE_ROWS if l == code and o)
    biz = BASELINE['bizwords'] if code == 'L1' else 0
    h4.append('<tr><td><b>%s</b></td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td></tr>'
              % (esc(title), cnt, 0, biz, hc, ov))
h4.append('</tbody></table>')
h4.append('<p class="kpi">读法：<b>L1 的「业务词 48 行」与 L3 的「硬编码 17 件」是两条主战线</b>。'
          '前者是语义越层，后者是配置面。表中数字为 top 榜口径（各层前 10 件），全量数字以门禁脚本输出为准。</p>')
secs.append('\n'.join(h4))

# 五、端口登记
body5 = ['<h2>五、端口登记表</h2>',
         '<p>规划文档判据 3 定的规矩：<b>一进程一端口一职责</b>。这张表是全集群同号约定，'
         '改动必须同步登记，否则「一端口多职责」会在无人察觉处复发。'
         '<span class="badge b-plan">会漂移</span> 落地前以各件源码的默认值为准。</p>',
         '<table><thead><tr><th>端口</th><th>职责</th></tr></thead><tbody>']
for k, v in sorted(PORT_ROWS, key=lambda x: -int(x[0])):
    body5.append('<tr><td><b>%s</b></td><td>%s</td></tr>' % (k, esc(v)))
body5.append('</tbody></table>')
body5.append("""<div class="callout"><b>登记表暴露的一个空位：</b>端口已登记，但<b>「哪个节点跑哪些件、怎么自启」
没有对应登记</b>。实查证据见第七节：仓库里只入了 3 个系统服务单元，本机实跑 6 个计划任务 ——
「跑着的」与「入仓的」不一致，这是静态入网的另一面。</div>""")
secs.append('\n'.join(body5))

# 六、入网接口契约
secs.append("""<h2>六、成员入网接口契约（草案 v0）</h2>
<div class="callout">
  <b>为什么放在阶段 0 一起评：</b>入网接口<b>本来就是通行层契约的一个成员</b>（它管的是「谁有资格收到信封」）。
  先定它，才不会在阶段 1–4 把适配层、网关、契约各做一遍之后，发现入网还要回头改通行层的边界。
  本草案是<b>接口形状</b>，不含实现；标注 <span class="badge b-plan">规划中（尚未落地）</span>。
</div>
<h3>6.1 三类调用者与信任级别</h3>
<table><thead><tr><th>调用者</th><th>拿什么证明</th><th>能用哪些端点</th><th>信任级别</th></tr></thead><tbody>
<tr><td>未入网成员</td><td>一次性邀请码</td><td>申请注册 / 查询进度 / 取引导包</td><td>最低：只能证明「有人给了我这串码」</td></tr>
<tr><td>待批准成员</td><td>自持私钥签名</td><td>查询进度 / 取引导包 / 自报上线</td><td>中：身份已登记，尚未授权</td></tr>
<tr><td>已入网成员</td><td>自持私钥签名</td><td>正式通道 + 名册只读</td><td>高：按角色模板授权</td></tr>
<tr><td>管理员</td><td>既有账号体系（横切身份）</td><td>批准 / 驳回 / 吊销 / 名册全量</td><td>最高：<b>不新造一套</b>，复用现有实现</td></tr>
</tbody></table>
<h3>6.2 端点清单（7 个）</h3>
<table><thead><tr><th>端点</th><th>方法</th><th>用途</th><th>幂等性</th></tr></thead><tbody>
<tr><td><code>/enroll/request</code></td><td>POST</td><td>提交注册申请：成员标识 + <b>公钥</b> + 声明能力 + 邀请码</td><td>幂等键 = 成员标识 + 公钥指纹；重复提交返回<b>同一</b>受理号</td></tr>
<tr><td><code>/enroll/{id}</code></td><td>GET</td><td>查进度：待批准 / 已批准 / 已驳回（附原因）</td><td>只读</td></tr>
<tr><td><code>/enroll/{id}/approve</code></td><td>POST</td><td>管理员批准（可选保留人工一环）</td><td>重复批准不改变结果</td></tr>
<tr><td><code>/enroll/{id}/deny</code></td><td>POST</td><td>管理员驳回（附原因，写入审计）</td><td>重复驳回不改变结果</td></tr>
<tr><td><code>/bootstrap/{bid}</code></td><td>GET</td><td>取引导包：一次性、短时效、<b>绑定公钥指纹</b></td><td>一包一次；取过即失效</td></tr>
<tr><td><code>/enroll/{id}/activate</code></td><td>POST</td><td>成员自报上线（带签名）→ 转「已入网」+ 写入网台账</td><td>重复上报返回首次上线时间</td></tr>
<tr><td><code>/members/{mid}/revoke</code></td><td>POST</td><td>吊销：即时失效于新连接</td><td>重复吊销不报错</td></tr>
</tbody></table>
<h3>6.3 数据形状（三个关键结构）</h3>
<table><thead><tr><th>结构</th><th>字段</th><th>说明</th></tr></thead><tbody>
<tr><td>注册请求</td><td>成员标识 · 公钥（ed25519）· 节点提示 · 声明能力 {后端类型, 模型, 工具体} · 客户端版本 · 邀请码</td><td><b>没有私钥、没有口令</b>。声明能力只是「自称」，不据此授权</td></tr>
<tr><td>签发结果</td><td>成员标识 · 通道参数 {模式, 端点, 主题前缀, 角色模板} · 同伴信息 · 引导包 {地址, 校验和, 版本, 有效期} · 签发时间 · 有效期</td><td>通道参数里<b>只有角色模板名，没有口令</b> —— 口令由成员侧用自持私钥向通道换取</td></tr>
<tr><td>引导包</td><td>通行层客户端件 · 本成员档案片段 · 自启模板 · 校验和清单</td><td>一次性 + 短时效 + 绑公钥。包内含<b>模板</b>不含生产凭据</td></tr>
</tbody></table>
<h3>6.4 七条硬约束（逐条可校验）</h3>
<table><thead><tr><th>#</th><th>约束</th><th>怎么校验</th></tr></thead><tbody>
<tr><td>1</td><td><b>私钥永不出机</b>：成员本机生成密钥对，只上报公钥</td><td>注册请求体里搜不到私钥字段；抓包/日志里无<code>私钥头标记</code></td></tr>
<tr><td>2</td><td><b>数据面与控制面分离</b>：控制面不可用时，已在网成员通信不受影响</td><td>停掉注册服务，跑一轮既有成员的点对点与群发，应全通</td></tr>
<tr><td>3</td><td><b>幂等注册</b>：同 (成员标识, 公钥) 重复申请返回同一身份</td><td>连发两次申请，比对受理号与最终签发结果一致</td></tr>
<tr><td>4</td><td><b>授权与登记分离</b>：登记 = 身份落库；授权 = 通道侧按角色模板放行</td><td>登记后未授权时，通道应拒绝其连接（而非接受后不投递）</td></tr>
<tr><td>5</td><td><b>吊销即时生效于新连接</b>，已建立连接在下一轮重连时失效</td><td>吊销后立刻发起重连，应被拒；旧连接的下一条消息应失败</td></tr>
<tr><td>6</td><td><b>引导包一次性</b>：过期/重复取用均拒绝</td><td>同一包连取两次，第二次应拒</td></tr>
<tr><td>7</td><td><b>不新造身份体系</b>：管理员鉴权复用横切身份实现</td><td>管理端点不出现第二套账号表；权限判定走既有角色</td></tr>
</tbody></table>
<h3>6.5 与现有件的对位（不用从零设计）</h3>
<ul>
  <li><b>角色模板的来源已有现成实现</b>：<code>gen_nats_conf.py</code> 现在就在为每个成员生成
      「只能发自己的心跳、收则放开」的权限白名单。<b>动态化 = 把这套生成逻辑从「部署时写进配置」
      搬到「注册时按需签发」</b>，模板形状照抄，不用重新发明。</li>
  <li><b>多成员托管已是既成事实</b>：本机一个守护进程托管 5 个成员身份（见第七节），
      所以入网服务要按<b>成员</b>粒度设计，不是按<b>进程</b>粒度 —— 否则同机的 5 个身份没法各入各的网。</li>
  <li><b>名册真源需要一次迁移</b>：现在名册靠订阅心跳动态收集；目标的 <code>/members</code>
      是登记库 + 心跳的合成视图，<b>不能两套并存</b>，否则「谁在网」有两个答案。</li>
  <li><b>通道侧需要一次性配置</b>：签发侧需要通道服务端开一次「外部授权」开关并指向本服务 ——
      实测通道服务端版本 <b>2.14.5</b>，满足外部授权方案的最低版本要求，
      <b>这条路在现网走得通，不必先升级</b>（探测方式：只读连客户端口读服务端握手信息；日期 2026-09-25）。</li>
</ul>
<h3>6.6 失败模式与兜底</h3>
<p>六种失败模式（引导包被截获 / 注册服务挂 / 伪装注册 / 重复注册 / 授权服务故障 / 版本漂移）
的逐条兜底见规划文档 6.2 节，本草案不重复。此处只加一条与台账相关的：<b>version 漂移必须可观测</b> ——
台账登记每个在网成员的客户端版本，注册服务拒收低于最低兼容版本的申请，并写审计。</p>""")

# 七、静态入网现场证据
secs.append("""<h2>七、静态入网的现场证据</h2>
<h3>7.1 一条启动参数里的全部耦合</h3>
<p>实查本机正在运行的成员守护进程（<code>Get-CimInstance Win32_Process</code> 读到的真实命令行，
此处按占位符口径改写，<b>不含真实口令</b>）：</p>
<pre><code>teamd.py --member "&lt;成员A&gt;,&lt;成员B&gt;,&lt;成员C&gt;,&lt;成员D&gt;,&lt;成员E&gt;"
        ↔ 每成员 1 组通道令牌参数   ← 5 个文件，逐个写进命令行
        ↔ 每成员 1 组通道口令参数   ← 5 个文件，逐个写进命令行
        --nats-host       "&lt;中枢&gt;:9930,&lt;第二入口&gt;:9930"  --nats-port 9930
        --a2a-base        "http://&lt;本机尾网地址&gt;:9920"   --admin-port 9960
        --blackboard-base "http://&lt;中枢&gt;:9940"           --workers 8</code></pre>
<table><thead><tr><th>观察</th><th>数字</th><th>含义</th></tr></thead><tbody>
<tr><td>一个进程托管的成员身份</td><td><b>5</b></td><td>同机多身份，入网服务必须按成员粒度设计</td></tr>
<tr><td>每个成员需要的凭据文件</td><td><b>2</b>（通道令牌 + 口令）</td><td>共 <b>10 个文件</b>手工摆放，无一处自取</td></tr>
<tr><td>启动参数里写死的拓扑</td><td>2 个通道入口 + 1 个黑板入口 + 1 个本机地址</td><td>换节点 = 改命令行</td></tr>
<tr><td>成员名与展示名的硬编码</td><td>出现在 <code>chat_svc.py</code> 的固定清单里</td><td>新增成员要改展示名清单，第 4 处要改</td></tr>
</tbody></table>
<h3>7.2 新增 / 删除一个成员的实际动作数</h3>
<table><thead><tr><th>动作</th><th>现在（静态配置）</th><th>目标（动态入网）</th></tr></thead><tbody>
<tr><td>1</td><td>通道服务端配置里加用户与口令</td><td>—（角色模板按需签发）</td></tr>
<tr><td>2</td><td>为本成员生成 2 个凭据文件并摆到各台机</td><td>—（引导包自取）</td></tr>
<tr><td>3</td><td>改成员守护的启动参数（成员名 + 2 组凭据 + 计数）</td><td>—（守护读名册）</td></tr>
<tr><td>4</td><td>改展示名硬编码清单</td><td>—（按能力声明渲染）</td></tr>
<tr><td>5</td><td><b>重启守护进程</b></td><td><b>一条注册请求</b>（守护无需重启）</td></tr>
<tr><td><b>合计</b></td><td><b>5 步、7 处改动、1 次重启</b></td><td><b>1 步、1 处、0 重启</b></td></tr>
</tbody></table>
<h3>7.3 为什么「重启会掐断别人」</h3>
<div class="callout warn">因为<b>一个守护进程托管 5 个成员身份</b>：为了给第 6 个成员加凭据而重启，
会把已经在网的 4 个成员的通道<b>一起断掉</b>。所以静态入网不只是「麻烦」——
它把<b>新增成员的代价转嫁给了存量成员</b>。这一条是规划文档把它列为「第四处最痛耦合」的实证依据，
也是阶段 5 优先级的来源。</div>
<h3>7.4 部署件与实跑件不一致（顺带查出的缺口）</h3>
<table><thead><tr><th>侧</th><th>实跑</th><th>入仓登记</th></tr></thead><tbody>
<tr><td>本机（Windows 节点）</td><td><b>6 个计划任务</b>：A2A 端点 / 成员守护 / 群投递桥 / CLI 后端 / 会话归档 / 台账巡检</td><td>0</td></tr>
<tr><td>Linux 节点</td><td>按同一套启动约定</td><td><b>3 个系统服务单元</b>（对话台 / 对话台公网实例 / 看板）</td></tr>
</tbody></table>
<p class="kpi">这张表说明：<b>「怎么起的」没有单一登记处</b>。阶段 0 台账先把它们记下来；
阶段 4（平台层 L0）要做的正是把「启动方式」也变成登记项，而不是散在计划任务与 shell 脚本里。</p>""")

# 八、门禁
secs.append("""<h2>八、门禁怎么跑（工具与基线）</h2>
<h3>8.1 两个新增工具件</h3>
<table><thead><tr><th>工具</th><th>作用</th><th>性质</th></tr></thead><tbody>
<tr><td><code>tools/hacs_layers.json</code></td><td>层归属表（机器可读）：件 → 层 / 端口 / 备注；层定义、端口登记、门禁规则</td><td>台账的孪生体，<b>唯一可编辑真源</b></td></tr>
<tr><td><code>tools/check_layers.py</code></td><td>五条检查的门禁脚本；<code>--json</code> 出机器可读报告，<code>--only 1,2</code> 跑子集</td><td>新增件，<b>零业务码改动</b></td></tr>
<tr><td><code>tools/gen_ledger_doc.py</code></td><td>从层归属表生成本台账 HTML（表格同源，防漂移）</td><td>新增件</td></tr>
</tbody></table>
<pre><code>python tools/check_layers.py           # 全量报告；退出码 0=无硬违规 / 1=有硬违规 / 2=不是源码树
python tools/check_layers.py --only 1  # 只跑硬门禁
python tools/check_layers.py --json    # 机器可读</code></pre>
<h3>8.2 五条检查 ↔ 七条判据</h3>
<table><thead><tr><th>检查</th><th>对应判据</th><th>级别</th><th>今日基线</th></tr></thead><tbody>
<tr><td>1 依赖方向</td><td>判据 2 依赖严格单向</td><td><b>硬（退出码 1）</b></td><td><b>0 违规</b></td></tr>
<tr><td>2 传输不含语义</td><td>判据 5 传输层不含业务词</td><td>提示级（需人判）</td><td><b>45 行命中</b>（多为注释；实质命中见 8.3）</td></tr>
<tr><td>3 配置零硬编码</td><td>判据 4 配置零硬编码</td><td>提示级</td><td><b>25 件</b>带字面量</td></tr>
<tr><td>4 存储句柄</td><td>判据 1 的前置检查</td><td>提示级</td><td><b>10 件</b>持句柄，<b>0 件越层</b>+<b>1 条已声明例外</b>（见 8.5）</td></tr>
<tr><td>5 跨件私有面访问</td><td>判据 1 契约先于实现</td><td>提示级（阶段 3 收官后若噪声仍为 0 即提为硬门禁）</td><td><b>0 处</b>（刀 14 收口后）</td></tr>
</tbody></table>
<p class="kpi">门禁末行会打出同一张对照：<code>判据对照：检查1↔判据2 · 检查2↔判据5 · 检查3↔判据4 · 检查4↔判据1 · 检查5↔判据1</code>。
判据 3 与判据 7 目前<b>尚无</b>对应的机械检查，是后续刀的缺口。</p>
<h3>8.3 提示级命中的人判结论（今日）</h3>
<ul>
  <li><b>检查 2 的 48 行命中里，实质越层只有两处</b>：成员 CLI 里的「会议召集」子命令
      （业务语义长在通行层）—— 边缘端点里的「上下文自愈 / 会话头注入」已在阶段 1 刀 1+2 消解
      （实现下沉到 L2 <code>agent_usage.py</code>），是本台账记下的第一处<b>已消解</b>越层。
      其余多为文档字符串与帮助文本 —— 这也是<b>它们只能是提示级</b>的原因。</li>
  <li><b>检查 3 最该先收的一条</b>：适配件里写死的运行时绝对路径（node 可执行文件路径）。
      这条会直接阻断「换 OS 部署」，属于判据 6。</li>
  <li><b>检查 4 的 2 件越层</b>：与规划文档「上下文水位三处口径」完全对位，见 4.2 的说明。</li>
</ul>
<h3>8.4 怎么用才不会变成形式</h3>
<ul>
  <li>硬门禁（检查 1）<b>进提交前检查</b>；提示级只在阶段收口时看趋势，<b>不要为了清零而清零</b>。</li>
  <li>每次拆件（阶段 1–5）后重跑，把 <code>--json</code> 结果按日期留档，用「越层数是否单调下降」
      衡量重构是否真的推进 —— 比「代码更好看了」可核。</li>
  <li>门禁报的件名以层归属表为准；<b>新增件必须先登记再写码</b>，否则门禁看不见它。</li>
</ul>""")

# 8.5 已声明的例外（从 hacs_layers.json 生成）
_r_ex = M['rules']['allowed_import'].get('exceptions', [])
_r_st = M['rules'].get('storage_handle_exceptions', [])
_rows = []
for _e in _r_ex:
    _rows.append(('<b>依赖方向</b><br><span class="kpi">下层 → 上层</span>',
                  '<code>%s</code> → <code>%s</code>' % (esc(_e['src']), esc(_e['dst'])),
                  esc(_e['reason']), esc(_e.get('expires_when', '')), esc(_e.get('recorded', ''))))
for _e in _r_st:
    _rows.append(('<b>存储句柄</b><br><span class="kpi">不该持的层</span>',
                  '<code>%s</code>' % esc(_e['file']),
                  esc(_e['reason']), esc(_e.get('expires_when', '')), esc(_e.get('recorded', ''))))
_body = '\n'.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % r for r in _rows) \
        or '<tr><td colspan="5">今日无例外</td></tr>'
secs.append("""<h3>8.5 已声明的例外（可审计、带到期条件）</h3>
<div class="callout warn"><b>例外的存在本身就是结论</b>：下表各条是「分层规则」与「现实约束」冲突时，
<b>显式登记</b>而不是把门禁调松 —— 门禁每次运行都会把例外原文打印出来，条目一多就该动架构，而不是动规则。</div>
<table><thead><tr><th>类型</th><th>对象</th><th>为什么允许</th><th>何时消解（到期条件）</th><th>登记于</th></tr></thead>
<tbody>
%s
</tbody></table>
<p class="kpi">机器可读登记处：<code>hacs_layers.json</code> → <code>rules.allowed_import.exceptions</code> /
<code>rules.storage_handle_exceptions</code>；门禁据此跳过判定，但<b>照常打印</b>。</p>""" % _body)

# 九、待确认
secs.append("""<h2>九、待确认（每项附推荐默认）</h2>
<table><thead><tr><th>#</th><th>待确认</th><th>推荐默认</th><th>不定的代价</th></tr></thead><tbody>
<tr><td>1</td><td>层归属表里那 4 个「跨两层」的件，拆分点是<b>现在就标注</b>还是先跑通业务再拆？</td><td><b>先标注、不拆</b>（本台账已标）—— 标注零成本，拆分有风险，先把边界说清</td><td>不标 = 每次讨论都重新吵一遍它属于哪层</td></tr>
<tr><td>2</td><td>提示级命中要不要进提交前检查？</td><td><b>暂不进</b>，先只做趋势登记</td><td>过早进 = 每次提交都在处理注释里的假命中</td></tr>
<tr><td>3</td><td>入网接口的「人工批准」一环保留还是全自动？</td><td><b>保留</b>（管理员一次点击），但设计成可一键关掉的开关</td><td>全自动 = 邀请码泄露即等于入网；永久人工 = 达不到你要的「零人工」</td></tr>
<tr><td>4</td><td>引导包要不要包含通行层客户端件本身？</td><td><b>包含</b>（否则新节点仍要人工放文件），但包必须一次性 + 短时效 + 绑公钥</td><td>不含 = 「自取文件」这一环落空</td></tr>
<tr><td>5</td><td>名册真源切换（心跳收集 → 登记库合成）的时机</td><td><b>阶段 5 内一次性切换</b>，不留两套并存期</td><td>并存 = 「谁在网」两个答案，排障时互相打架</td></tr>
<tr><td>6</td><td>本台账与规划文档的关系，是否要合并成一份？</td><td><b>分开</b>：规划是稳定的「为什么」，台账是高频变化的「现状与工具」</td><td>合并 = 每次改层归属都要动权威设计文档</td></tr>
<tr><td>7</td><td>下一步先做哪期？</td><td><b>阶段 1 抽适配层</b>（台账已把越层点指出来，改动面最小、收益最直接）</td><td>先做网关或通行层 = 动的面更大，回滚更贵</td></tr>
</tbody></table>
<div class="callout"><b>本台账不含任何真实凭据。</b>文中出现的地址、成员名一律为占位符口径；
第七节的进程实参为改写后的等价形式（结构与参数个数真实，<b>口令/令牌值已剔除</b>）。
台账与规划文档同属长期参考件，<b>正文不含密钥、口令、令牌、连接串</b>，凭据一律只记录「在哪个路径」。</div>""")

# SVG：入网六步（注意：SVG 的 text 里不能出现 HTML 标签，强调只许用 tspan）
svg_steps = [
    ('1', '身份本机生成', '成员本机生成密钥对；', '私钥永不出机', '，只上报公钥与声明能力'),
    ('2', '提交注册申请', '带一次性邀请码 → 幂等键 = 成员标识 + 公钥指纹；', '重复申请返回同一受理号', ''),
    ('3', '按角色模板授权', '批准后由登记库向通道侧签发角色（', '模板形状复用现有白名单生成逻辑', '）'),
    ('4', '引导包自取', '一次性 + 短时效 + 绑定公钥：', '通行层客户端件 + 本成员档案 + 自启模板', ''),
    ('5', '自报上线 + 入台账', '带签名自报 → 名册转「已入网」→ ', '写入网台账（含客户端版本）', ''),
    ('6', '吊销 = 一条请求', '即时失效于新连接；旧连接在下一轮重连时失效；', '不重启任何守护进程', ''),
]
_svg_rows = []
y0 = 62
for i, (no, title, pre, hl, post) in enumerate(svg_steps):
    y = y0 + i * 56
    if hl:
        desc = ('<tspan>%s</tspan><tspan font-weight="700" fill="#1f4e79">%s</tspan>'
                '<tspan>%s</tspan>' % (pre, hl, post))
    else:
        desc = '<tspan>%s</tspan>' % pre
    _svg_rows.append(
        '<rect x="24" y="%d" width="46" height="40" rx="10" fill="#2b6cb0"/>'
        '<text x="47" y="%d" font-size="18" font-weight="700" fill="#ffffff" text-anchor="middle">%s</text>'
        '<rect x="84" y="%d" width="1130" height="40" rx="8" fill="#f7fafd" stroke="#dfe6ef"/>'
        '<text x="104" y="%d" font-size="14" font-weight="700" fill="#1f4e79">%s</text>'
        '<text x="360" y="%d" font-size="12.5" fill="#43536a">%s</text>'
        % (y, y + 26, no, y, y + 26, title, y + 26, desc))
SVG = ('<figure><svg viewBox="0 0 1240 456" role="img" aria-label="成员入网六步">'
       '<rect x="0" y="0" width="1240" height="456" fill="#ffffff"/>'
       '<text x="24" y="30" font-size="17" font-weight="700" fill="#1f4e79">成员入网六步 · 目标形态</text>'
       '<text x="24" y="50" font-size="12" fill="#6b7c95">'
       '全程无人工配置；每条都对应第六节的硬约束与校验方法（规划中，尚未落地）</text>'
       + ''.join(_svg_rows)
       + '<rect x="84" y="%d" width="1130" height="46" rx="8" fill="#fdf3e0" stroke="#eccf9a"/>'
         '<text x="104" y="%d" font-size="13" font-weight="700" fill="#8a5300">'
         '唯一的一次性改动</text>'
         '<text x="360" y="%d" font-size="12.5" fill="#5a4620">'
         '由通道服务端开一次「外部授权」开关并指向本服务；实测服务端版本满足要求，不必先升级</text>'
       % (y0 + 5 * 56 + 50, y0 + 5 * 56 + 80, y0 + 5 * 56 + 80)
       + '</svg><figcaption>图 1 · 成员入网六步（目标形态）</figcaption></figure>')
secs_doc = []

HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HACS 分层台账 · 阶段 0 产出</title>
<style>
  :root{
    --ink:#1b2430; --ink2:#43536a; --ink3:#6b7c95;
    --line:#dfe6ef; --bg:#f5f7fa; --card:#ffffff;
    --accent:#2b6cb0; --accent2:#1f4e79;
    --ok:#1f7a4d; --okbg:#e6f5ec;
    --plan:#8a5300; --planbg:#fdf3e0;
    --warn:#8a1f1f; --warnbg:#fbeaea;
  }
  *{box-sizing:border-box}
  body{
    margin:0; padding:0 0 60px;
    background:var(--bg); color:var(--ink);
    font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  }
  .wrap{max-width:1080px;margin:0 auto;padding:0 22px}
  header{
    background:linear-gradient(135deg,#1f4e79,#2b6cb0);
    color:#fff; padding:34px 0 26px; margin-bottom:26px;
  }
  header h1{margin:0 0 8px;font-size:27px;letter-spacing:.5px}
  header .sub{opacity:.92;font-size:14px}
  .verline{
    margin:16px 0 0; padding:11px 14px; border-radius:8px;
    background:rgba(255,255,255,.13); font-size:13px; line-height:1.9;
  }
  h2{
    font-size:20px; margin:34px 0 12px; padding:8px 0 8px 12px;
    border-left:5px solid var(--accent); background:#fff;
    border-radius:0 6px 6px 0;
  }
  h3{font-size:16px;margin:22px 0 8px;color:var(--accent2)}
  p{margin:9px 0}
  ul,ol{margin:8px 0 8px 4px;padding-left:22px}
  li{margin:5px 0}
  code{
    font:13px/1.5 Consolas,"Cascadia Mono",monospace;
    background:#eef2f7;padding:1px 5px;border-radius:4px;color:#1f4e79;
  }
  pre{
    background:#1b2430;color:#e6edf5;padding:14px 16px;border-radius:8px;
    overflow-x:auto;font:12.5px/1.7 Consolas,"Cascadia Mono",monospace;
  }
  pre code{background:none;color:inherit;padding:0}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}
  table{width:100%;border-collapse:collapse;margin:12px 0;background:#fff;font-size:14px}
  th,td{border:1px solid var(--line);padding:8px 10px;vertical-align:top;text-align:left}
  th{background:#eef2f7;color:var(--accent2);font-weight:600;white-space:nowrap}
  tbody tr:nth-child(even){background:#fafcfe}
  .badge{
    display:inline-block;padding:1px 8px;border-radius:20px;
    font-size:12px;font-weight:600;white-space:nowrap;
  }
  .b-plan{background:var(--planbg);color:var(--plan);border:1px solid #eccf9a}
  .b-ok{background:var(--okbg);color:var(--ok);border:1px solid #a9dcc0}
  .b-warn{background:var(--warnbg);color:var(--warn);border:1px solid #eebdbd}
  .b-info{background:#e8f1fb;color:var(--accent2);border:1px solid #b9d4ee}
  .callout{border-left:4px solid var(--accent);background:#eef5fc;padding:12px 15px;border-radius:0 8px 8px 0;margin:14px 0}
  .callout.warn{border-color:#c05621;background:#fdf6ec}
  .callout.now{border-color:#1f7a4d;background:#effaf3}
  figure{margin:18px 0;background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px}
  figure svg{display:block;width:100%;height:auto}
  figcaption{font-size:13px;color:var(--ink3);margin-top:8px;text-align:center}
  .kpi{font-size:13px;color:var(--ink2)}
  .kpi b{color:var(--accent2);font-size:15px}
  footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--line);font-size:13px;color:var(--ink3)}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>HACS 分层台账</h1>
    <div class="sub">阶段 0 产出 · 件 → 层 → 端口 → 契约 → 门禁基线的唯一权威版本</div>
    <div class="verline">
      版本 <b>v13</b> · 2026-09-25 首版 / 2026-09-27 阶段 2–4 全闭 · 阶段 5 刀25（只读对账）→ 刀26（退场/收敛）→ 刀27（名册档案化 + 逐字节自证）→ 刀28（对账器身份口径）· 配套 <code>docs/HACS-分层规划-handoff.html</code>（规划文档，回答「为什么这么分」）<br>
      本次变更（v13）：阶段 5 <b>第四刀 · 对账器的「身份 / 观测量」分离</b>（刀28）—— 刀25 那台只读对账器要当收口刀的<b>验收门禁</b>用，可它先把我们坑了一次：<code>--check</code> 的「发现项身份」里混进了<b>随钟漂移的观测量</b>（<code>stale_routes</code> 由墙钟决定：NATS 每次重启后路由 idle 归零、约 1 小时必由 0 变正），于是同一裁决每轮都报成「新增 + 消失」成对出现 → 基线<b>假红</b>（实测 rc=1）。本刀把身份定义为<b>判据的裁决</b>（哪项检查 / 哪类违规 / 落在哪个对象上），观测数字递归摘出（<code>VOLATILE_KEYS</code>，只登记 <code>stale_routes</code>）；<b>判据一条未放宽</b> —— 裁决消失/新增、非观测字段（<code>num_routes</code> 4→8）漂移照旧报漂移，装置新增的 D 段 6 格把这三点硬钉住。证据：① 门禁 <code>rc=0</code> · 件 <b>77 → 78</b> / 边 <b>91 → 94</b>（新增「刀28 前的正件逐字节快照」当装置锚）/ 例外 0 · 检查 5 = 0 处 ② 装置 <b>112 格 0 不一致</b>（106 → 112）· 两轮同 md5 <code>14c1fca8…</code> · <code>EQUIV_SENTINEL_OK</code>；负控注毒 3 处双侧全红、还原转绿 ③ 红点 <b>40 == 40</b>（改前树 <code>git archive HEAD agent-team</code> 独立解包；集合 diff 0 行）· 扫密 <b>40 == 40</b>（本刀新增文件 0 命中）④ 基线重冻结<b>过账</b>：只有 <code>findings.M6[0].hubs[*].stale_routes</code> 0→2 变，全部 <code>COMPARE_KEYS</code> 的判据身份逐字节不变；<code>--check</code> 由假红 <b>rc=1 → rc=0</b>，M6 那条<b>仍在</b> ⑤ 本刀<b>不改任何运行期件、不部署</b>（只读巡检件，<code>tools/</code> 不进部署表）→ <code>deploy.py --status / --health-only</code> <b>没跑</b>（无部署面可比对）。<br>
      上一轮（v12）：阶段 5 <b>第三刀 · 名册档案化 + 「档案 + 凭据探针 ⇒ 用户区」逐字节自证</b>（刀27）—— 阶段5 的收口刀要让生成器<b>按档案自动写 NATS 配置</b>；在那之前必须先证明一件事：<b>L0 名册档案 + 各机凭据文件，够不够唯一决定现网中枢的用户区</b>。本刀<b>不动 NATS、不改任何运行期件、不部署</b>，只做这个证明。<br>
      ① <b>新增 L0 名册档案</b> <code>agent-team/member_roster.py</code> —— 从本刀起它是<b>名册的唯一真源</b>，<code>nats.conf</code> 的用户区降级为它的<b>产物</b>（刀25 把名册真源定义为「中枢 conf 的规范形」并写明「提升为独立档案件是后续刀的事」，本件就是那个落点）。<b>有序</b>表，顺序 <b>==</b> conf 里 users 的规范序（字典序）—— 这是逐字节对照成立的前提；条目形态冻结为 <code>(member, role, cred_node, cred_kind)</code> 四元组。<b>三条铁律</b>：口令绝不进档案（只记「真值在哪台机的哪个文件」）· 不写路径 / 不写 IP（<code>cred_kind</code> 是位置的<b>种类</b>不是路径）· 不存权限串（角色模板唯一出处仍是 L1 <code>gen_nats_conf.perm_list</code>，故本件<b>不许 import 它</b> —— L0 → L1 是方向硬违规）。纯数据 + 纯查询：<b>零 import、零 IO、零环境依赖</b>。<br>
      ② <b>新增自证器</b> <code>tools/roster_derive_conf.py</code>（T 工具面）。判据：「档案有序表 + 各成员凭据文件内容 → 按 <code>gen_nats_conf</code> 行格式渲染的用户区」<b>==</b>「两中枢 <code>nats.conf</code> 里原样抽出的用户区」，<b>归一化后逐字节相等</b>。<b>用户区定义冻结</b>：从含 <code>users = [</code> 的那个 <code>authorization {</code> 行起到配对的 <code>}</code> 行止（含首尾、不重排不 strip）—— <code>cluster</code> 块里那条单行 <code>authorization</code> 没有 <code>users</code>，<b>集群路由凭据天然落在区外</b>。<b>归一化只许两处</b>：口令换 <code>sha256:</code>前 12、生成时间戳行换固定形态（后者在用户区上<b>永不命中</b>，写进契约是为将来扩面 —— 如实记账）。退出码 <b>0</b> 逐字节一致 / <b>1</b> 有差异（逐行打印）/ <b>2</b> 采集失败（<b>绝不退化成 0 或 1</b>）。只读：远端<b>只发 <code>cat</code> 一条形态</b>。<br>
      ③ <b>真机实测（两中枢 lobster / top001）</b>：档案 <b>8</b> 人；<code>lobster</code> 用户区 <b>✓ 一致</b>、<code>top001</code> 用户区 <b>✓ 一致</b>，两侧归一化 md5 均 <b><code>970bcf1c</code></b>、各 <b>13 行</b>、差异 <b>0 行</b>，<code>collect_errors</code> 空、<code>ok = true</code>、<b>退出码 0</b>。<b>「档案 + 探针 ⇒ 用户区」现在是被机械证明的事实，不是设计意图。</b><code>--json</code> 全文里 <code>password</code> 字样 <b>0</b> 次（零差异 ⇒ 无 <code>diffs</code> 行，出口面连摘要都不需要出现）。<br>
      ④ <b>等价证据</b> <code>tools/equivalence/roster_derive_equiv.py</code>（新增）：<b>50 格 × 2 轮</b>，两轮矩阵指纹逐字节相同 <code>37dbd959e3e7a13d5126a1ac3dddf4ce</code>（<code>EQUIV_SENTINEL_OK</code> / <code>EQUIV_DETERMINISM_OK</code>）。A 段 12 格角色模板互证；<b>B 段 7 格抽块器互证</b>（正件「定位 <code>users</code> 行再<b>向上回溯</b> + 逐字符配平」 vs 旧侧「先切<b>顶层区块</b>再挑体内含 <code>users</code> 的 <code>authorization</code> 块」，含空文本 / 无 users / users 在非 authorization 块里 / 花括号未配平 <b>4 例畸形</b>，块文本与说明文案都要一致）；<b>B 段另 7 格归一化器互证</b>（正则 vs 零正则手写扫描器）—— <b>单列的理由是一处如实认下的覆盖缺口</b>：归一化第 2 条「生成时间戳行」在用户区上<b>永不命中</b>，只靠 C 段夹具它就是<b>零覆盖</b>（实现了但从没被验过），故直接对文本比两侧 <code>normalize()</code> 把缺口补上；其中「形似时间戳但缺后缀」那一格<b>不被归一</b>，确认这条规则是紧的、不会误伤。C 段 24 格 =<b>6 例冻结夹具</b>（<code>clean</code> 0 差异 rc0 / <code>order_swap</code> 2 行 rc1 / <code>cred_mismatch</code> 1 行 rc1 / <code>missing_cred</code> <b>rc2 + collect_errors 非空</b> / <code>extra_in_conf</code> 4 行 rc1 / <code>perm_drift</code> hub-a 1 行 hub-b 0 行 rc1）× 4。<code>--negative</code> 注毒 3 处（改一个凭据内容 / 换档案顺序 / 删 conf 里一个用户）<b>两侧全红</b>、还原<b>转绿</b>。旧侧 <code>_old/roster_derive_old_k27.py</code> 走<b>另一条算法路径</b>（顶层切块 + <b>零正则</b>手写扫描器 + 逗号后补 + 模板自拼），不 import 正件、不 import <code>gen_nats_conf</code>。<b>全程离线</b>：夹具内嵌源码、跑时落临时目录跑完即删；主机名一律 <code>*.example.invalid</code>、口令一律 2 字符合成串。<br>
      ⑤ <b>下一刀的前置缺口（已量出，本刀不实现）</b>：<b>中枢机上的生成器拿不到全体成员的凭据</b> —— 现有凭据<b>分布在各成员自己那台机</b>上（5 份在 <code>thinkbook</code> 运行目录，3 份各在 <code>lobster</code> / <code>quantum</code> / <code>top001</code> 配置目录），而中枢上的 <code>gen_nats_conf.py</code> 只能从旧 conf <b>原样继承</b>口令。<b>推荐默认</b>：中枢侧建 <code>&lt;conf_dir&gt;/creds/&lt;member&gt;.pass</code> 镜像，由部署件<b>单向</b>（节点 → 中枢）同步，口令不入仓不入日志，权限 0600/0700；同步后 <code>roster_derive_conf.py --live</code> 必须仍 rc=0 —— <b>它就是这条通道的验收门禁</b>。顺序不可颠倒：先建凭据通道 → 再切生成器 → 最后才切对账器期望侧。契约见 <code>agent-team/docs/l5-roster-archive.md</code>。<br>
      ⑥ <b>门禁与基线</b>：<code>check_layers.py</code> <b>rc=0</b>、检查 1 无反向依赖、<b>已声明例外 0</b>；登记件数 <b>73 → 77</b>、层间 import 边 <b>84 → 91</b>（本刀自身 = 件 <b>+4</b>、边 <b>+7</b>）。<b>如实记一处继承订正</b>：本节常量此前停在 <b>72</b>，而刀26 登记 <code>gen_nats_conf_equiv.py</code> 时实际已到 <b>73</b> —— 当时只重生成了 HTML 没同步本常量，本刀一并订正到与 <code>check_layers.py</code> 实时打印值同源同数。<b>本刀不改任何运行期件、不部署 → 部署表受管件数不变、无需重启</b>（<code>member_roster.py</code> 进部署表是<b>切生成器那一刀</b>的事）。<br>
      上一轮（v11）：阶段 5 <b>第一刀 · 成员入网「只读对账」</b>（刀25）—— 阶段5 的目标是把「成员入网」从人工改 NATS 配置变成<b>按档案 + 探针自动生成</b>；本刀<b>不动 NATS、不改任何运行件</b>，只先把「现网究竟长什么样」量出来并<b>冻结成基线</b>。① 新增只读对账器 <code>tools/nats_membership_audit.py</code>（T 工具面，898 行）六项检查：<b>M1</b> 两中枢 (user → 口令) 矩阵逐字节一致 · <b>M2</b> 每用户权限串 <b>==</b> L1 <code>gen_nats_conf.perm_list(user)</code> 逐字符（角色模板<b>唯一出处</b>，本件不另抄一份）· <b>M3</b> 各机 <code>*.pass</code> 内容 == 名册口令 · <b>M4</b> 名册成员的「凭据文件 ∪ 活连接」落地覆盖（两者皆无 = <b>僵尸凭据</b>）· <b>M5</b> 活连接 ⊆ 名册 · <b>M6</b> 遗留中枢（无 <code>permissions</code> 块 / 非 v2 端口）与集群路由重复。采集面两条：<code>--live</code>（经 ssh <b>只发</b> <code>cat</code> / <code>ls -1</code> / <code>curl -s</code> GET）与 <code>--fixtures</code>（冻结夹具，供装置离线跑）。三条铁律：<b>① 只读</b> —— 无任何写操作、不重启单元、不改 <code>nats.conf</code>；<b>② 口令一个字节都不出门</b> —— 采集器当场换 sha256，明文连 artifacts 都进不去（实测：用 5 个真口令对 <code>--json</code> 全量输出做泄漏测试，<b>0 命中</b>）；<b>③</b> <code>analyze()</code> 是<b>纯函数</b>（无 IO / 无 time），角色模板由参数注入 —— 正件在 <code>main()</code> 传 L1 <code>perm_list</code>、装置传它自己那份独立实现，两路互证。<br>
      ② <b>真机实测（本刀基线；两中枢 = lobster / top001）</b>：名册 <b>9</b> 名成员；<b>M1 差集 0 · M2 漂移 0 · M3 失配 0</b>（含本机 5 件 <code>nats-*.pass</code>）<b>· M5 越界 0</b>；<b>M4 = 1</b>（<code>bbgw</code>：全仓无 <code>.pass</code> 文件、<code>/connz</code> 零活连接、无任何消费者 —— 僵尸凭据）；<b>M6 = 2</b>（<code>quantum</code> 上另有一份遗留 <code>nats-server</code>：4222 + 8222 监控 + TLS + JetStream、<b>5 个用户且整份配置没有 <code>permissions</code> 块 = 全权</b>；两中枢 <code>/routez</code> 各报 <code>num_routes=4</code>，其中 3 条陈旧（idle 6d18h / 1d9h / 5h55m）= 路由重复）。这 <b>3 条非零项如实冻结进基线、不改口径消红</b>，留给阶段5 后续刀去消（吊销 <code>bbgw</code> / 收敛 quantum 4222 / 路由去重），本刀只记账。<br>
      ③ <b>基线冻结机制</b>：结果写 <code>tools/nats_membership_audit_baseline.json</code>，<code>--check</code> 与基线<b>逐项比</b>（退出码 0 = 一致 / 1 = 有漂移 / 2 = 采集失败）。独立负控实测：抹掉基线里那条 <code>bbgw</code> → 报「M4 新增」且 rc=1；凭空加一条 <code>M5</code> → 报「M5 消失」且 rc=1 —— <b>基线变更因此必须显式过账</b>（连「消掉一项」都会立刻报红催更新基线），从机制上杜绝「悄悄放宽」。<br>
      ④ <b>等价证据</b> <code>tools/equivalence/membership_audit_equiv.py</code>（新增）：<b>106 格 × 2 轮</b>，<code>--runs 2</code> 两轮输出逐字节相同（<code>EQUIV_SENTINEL_OK</code> / <code>EQUIV_DETERMINISM_OK</code>）；A 段角色模板互证（正件走 L1 <code>perm_list</code> vs 旧侧自拼，12 成员逐字符）；B 段 <b>10 例冻结夹具</b>（clean / zombie / cred_mismatch / cred_orphan / perm_drift / hub_divergence / outside_roster / legacy_hub / dup_route / malformed）逐例比「新侧规范 JSON ↔ 冻结期望」且<b>新旧两侧互比</b>；<code>--negative</code> 强制负控 3 处注毒（改一个口令 / 加一个名册外活连接 / 删 <code>permissions</code> 块）<b>两侧都必须报红</b>、还原转绿。旧侧 <code>tools/equivalence/_old/membership_audit_old_k25.py</code> 走<b>另一条算法路径</b>（逐行状态机 + 顶层逗号切词），不 import 正件、不 import <code>gen_nats_conf</code>。<br>
      ⑤ <b>红点</b> 改前 = 改后 = <b>39</b>（独立解包 <code>git archive HEAD agent-team</code> 到临时树再跑，不是同一副本自比）；<b>门禁</b> <code>check_layers.py</code> <b>rc=0</b>、检查 1 无反向依赖。<b>本刀不改任何运行期件 → 无部署步骤</b>（部署表受管件数不变、无需重启）。<br>
      ⑥ <b>3.2 基线重算（含一处历史漂移的如实订正）</b>：登记件数 <b>65 → 72</b>、层间 import 边 <b>52 → 84</b>。其中 <b>4 件 / 27 条边是本刀之前就已发生的漂移</b>（刀15–刀24 期间件数 65 → 69、边 52 → 79，历次只同步了第二章那句「共登记 N 个件」而漏掉本节），本刀一次订正到与 <code>check_layers.py</code> 实时打印值<b>同源同数</b>，并把本节的「含义」列改成口径自明；本刀自身贡献 = 件 <b>+3</b>、边 <b>+5</b>（<code>nats_membership_audit</code> → <code>node_profile</code> / <code>gen_nats_conf</code>；装置 → 正件 / 旧侧 / <code>gen_nats_conf</code>）。<br>
      上一轮（v10）：阶段 3 <b>刀 14 · 公开面收口 + 门禁检查 5</b> —— ① 消除全仓<b>唯一</b>跨件私有面访问：L2 <code>agent_model.py</code> 增公开面 <code>DISPLAY_NAMES_FALLBACK</code>（<b>与私有原名 <code>_DISPLAY_NAMES_FALLBACK</code> 同一个 dict 对象</b>，不是拷贝；原名<b>保留不动</b>，<code>__all__</code> 追加），L3 <code>chat_svc.py</code> 第 102 行由 <code>agent_model._DISPLAY_NAMES_FALLBACK</code> 改引 <b>公开名</b> —— <b>零行为改变</b>（<code>is</code> 同一对象双向自证 True）。② <code>tools/check_layers.py</code> 新增 <b>检查 5 · 跨件私有面访问</b>（<b>提示级</b>，不参与退出码），对应<b>判据 1「契约先于实现」</b> —— 这是判据 1 的<b>第二条可机械校验</b>的门禁（检查 4 是它的前置）；口径用 <code>ast</code> 只认<b>真实属性访问</b>（<code>ast.Attribute</code> + <code>ast.Name</code> 且别名来自本仓登记件），注释/文档串/字符串里的 <code>mod._xxx</code> 提及<b>一律不算</b>（与旧正则扫描的关键区别），<code>__x__</code> dunder 豁免，<code>layer == "T"</code> 的测试件豁免。改前全仓<b>恰好 1 命中</b>，改后 <b>0 处</b>。阶段 3 收官后噪声仍为 0 即提为硬门禁（<b>本刀不提</b>）。③ 件数 <b>65</b> / 层间 import 边 <b>52</b> <b>均不变</b>（本刀不增删件、不改层归属）；<code>check_layers.py</code> 检查 1–4 输出与改前逐行一致（唯一差异 = <code>chat_svc.py</code> 因加了 1 行注释，检查 3 报的行号 4495 → 4496）。<br>
      上一轮（v9）：① 阶段 2 <b>刀 13 · 群聊前端「业务视图」抽出</b>（前端拆三件套<b>收官刀</b>）—— 新件 <code>agent-team/ui/group_view.js</code>（<b>74858 B</b> / md5 <code>67d3ea72f56c4679b63eec7bdfcc79d6</code> / 1568 个换行），内容是 <code>group.html</code> 那个<b>唯一的内联 <code>&lt;script&gt;</code> 块</b>（第 102–1671 行）里 offset <code>[4896, 79754)</code> 的<b>逐字节</b>搬运（不重排、不格式化、不加头注释、不加 BOM、<b>不包 IIFE</b>、<b>不加 <code>"use strict"</code></b>、首末字节仍是换行）；<code>group.html</code> 把 offset <code>[4887, 79764)</code> 那 <b>74877 B</b> 的 <code>&lt;script&gt;</code>…<code>&lt;/script&gt;</code> 整块换成<b>单行 39 B</b> 的 <code>&lt;script src="/group_view.js"&gt;&lt;/script&gt;</code>，79780 → <b>4942 B</b> / md5 <code>408a8b649dec3986636d3b3327573c54</code>（字节账 79780 − 74877 + 39 = 4942 ✓）。外链行落在<b>原内联块同一 offset 4887</b>（紧接 <code>/group_transport.js</code> 之后、<code>&lt;/body&gt;</code> 之前）—— 外链<b>经典</b>脚本仍是 parser-blocking、在原位置执行，<b>文档序即执行序，不变</b>（该块顶部那句「状态声明必须放脚本最前面」的顺序依赖因此成立）。<b>注意字节账的一处反直觉</b>：<code>&lt;/script&gt;</code> 计数 <b>6 → 6 不减</b> —— 外链 script <b>不是空元素</b>，<code>&lt;script src=…&gt;&lt;/script&gt;</code> 自带闭标签，与刀12 的 <code>&lt;link&gt;</code>（空元素，<code>&lt;/style&gt;</code> 1 → 0）不同；真正该断言的是<b>内联 <code>&lt;script&gt;</code> 计数 = 0</b>、<code>&lt;script src=</code> 计数 = <b>6</b>、外链行计数 = <b>1</b>。零 <code>.py</code> 改动、零 JS 语义改动。至此 <code>group.html</code> 只剩壳：HTML 骨架 + 1 个样式外链 + 6 个脚本外链，<b>零内联 <code>&lt;style&gt;</code>、零内联 <code>&lt;script&gt;</code></b>。<br>
      ② 等价证据 <code>tools/equivalence/group_frontend_equiv.py</code> <b>再扩用</b>（只加判据不放宽）：静态路由表加 <code>/group_view.js</code>；<code>--old</code> 默认值改 <code>_old/group_old_k13.html</code>（<b>79780 B</b> / <code>58497402…</code>，开工前逐字节快照），并对该旧侧<b>硬校验 md5</b> 防拿改过的件当旧侧。新增 <b>第 0b 类 · JS 文本级（业务视图）</b>（与刀12 第 0 类 CSS 文本级同构）：旧快照唯一内联 script 块的内容 <b>==</b> <code>group_view.js</code> 逐字节（74858 B / <code>67d3ea72</code>），外加字节账与<b>重组互补证据</b>（把新件那 39 B 换回原 74877 B 整块 → 与旧快照<b>逐字节相等（79780 B / <code>58497402</code>）</b>）；<b>反向证据</b>加一条（防「外链没加载，两个空页面也算一致」）：新侧必须真的 <code>GET /group_view.js</code>、旧侧<b>一次都不许请求</b>（三份旧快照里脚本都还是内联的 → 这是条无条件强断言）。原有判据<b>一条没删</b>：计算样式 <b>87 元素 × 92 属性 = 8004 个值逐行 diff 0 行</b>、CSSOM 两侧 <b>233 规则 / 24425 字符 / md5 同</b>、JS 文本级（刀11 锚）5/5、标识符 9/9、启动 4/4、轮询 1/1、动作序 13/13、函数级 7 例线级逐字节、两侧 JS 错误 0/0、客户端身份（UA / UA-CH / Accept-Language）钉死 —— 逐条 diff 合计 <b>0</b>，<code>EQUIV_SENTINEL_OK</code>；<code>--runs 2</code> 两次全 OK 且归一端口后<b>两份输出逐字节相同</b>（md5 <code>97ac0740538a9f5b7d286b8c28e4ba91</code>，10322 B）→ <code>EQUIV_DETERMINISM_OK</code>。<br>
      ③ <b>第 0 类 CSS 文本级换锚（如实记账，非放宽）</b>：该判据原先比的是「<code>--old</code> 给的旧侧 vs 活件」。刀13 又动了 <code>group.html</code>，活件与 k12 快照之间隔了<b>两步</b>，再拿默认旧侧（k13 快照）去找 <code>&lt;style&gt;</code> 段只会找不到 —— 那不是判据失效，是<b>锚错了位置</b>。故把它钉死成 <b>k12 快照 ↔ k13 快照</b>这对冻结字节（与 JS 文本级钉 k11 快照同一套路），<b>期望数字一个没下调</b>（31078 B / 31095 B / 42 B / offset 173 / <code>&lt;/style&gt;</code>=0 / <code>&lt;link</code>=1 / 110833 B 全部照旧），并<b>追加一条全链重组</b>：从<b>活的</b> <code>group.html</code> 两步走回 k12 快照（<code>408a8b64 → 58497402 → e5c46769</code>）逐字节相等 —— 活件不因换锚而脱钩。同理，「OLD 侧必须有内联 <code>&lt;style&gt;</code>」与「OLD 侧一次没请求过 <code>/group.css</code>」两条按旧侧快照形态<b>分派</b>：传 k12/k11 时走原文一字未改，传 k13（样式已外链）时换成<b>同强度的正面断言</b>（OLD 侧也必须有且仅有一张 <code>/group.css</code> 表且规则数 &gt; 0、也必须取过它），没有任何一侧被放过。<br>
      ④ <b>3.2 基线重算</b>：登记件数 64 → <b>65</b>（新增 L4 <code>group_view.js</code>）、层间 import 边 <b>52</b>（不变 —— 前端静态件之间没有 import 边）。部署表 lobster 段受管件 21 → <b>22</b>（<code>ui/group_view.js</code>，<code>services=[]</code>，静态件免重启）；入仓清单 124 → <b>126</b> 条（新件 + <code>_old/group_old_k13.html</code> 快照）。<br>
      上一轮（v8）：① 阶段 2 <b>刀 12 · 群聊前端「渲染壳·样式表」抽出</b>（前端拆三件套第二刀）—— 新件 <code>agent-team/ui/group.css</code>（<b>31078 B</b> / md5 <code>ea71fd30af6a49a7365ac2d643226ad5</code>），内容是 <code>group.html</code> 内联 <code>&lt;style&gt;</code> 段 offset <code>[181, 31259)</code> 的<b>逐字节</b>搬运（不重排、不格式化、不加注释、不加 BOM、行尾仍是 LF）；<code>group.html</code> 把 offset <code>[173, 31268)</code> 那 <b>31095 B</b> 的 <code>&lt;style&gt;</code>…<code>&lt;/style&gt;</code> 整段换成<b>单行 42 B</b> 的 <code>&lt;link rel="stylesheet" href="/group.css"&gt;</code>，110833 → <b>79780 B</b> / md5 <code>58497402129900f8dfa0e7b513bd3afe</code>（字节账 110833 − 31095 + 42 = 79780 ✓；<code>&lt;/style&gt;</code> 计数 <b>0</b>、<code>&lt;link</code> 计数 <b>1</b>）。<code>&lt;link&gt;</code> 落在<b>原 <code>&lt;style&gt;</code> 同一 offset 173</b>（<code>&lt;/title&gt;</code> 之后、<code>&lt;/head&gt;</code> 之前）—— 两者同为 author-origin、文档序定胜负，<b>同位置即同层叠顺序</b>。零 <code>.py</code> 改动、零 JS 改动、HTML 骨架与事件绑定一字未动。剩「业务视图」待第三刀。<br>
      ② 等价证据 <code>tools/equivalence/group_frontend_equiv.py</code> <b>扩用</b>（只加判据不放宽）：静态路由表加 <code>/group.css</code>（不加则新侧外链 404 → 必然假红）；旧侧改成 <code>--old</code> 参数（默认 <code>_old/group_old_k12.html</code>），原来写死的「旧侧 md5 前缀 = <code>c7ff8b4e</code>」降级成可选的 <code>--old-md5</code>，「新旧 md5 必须不同」的防自比照旧。新增三类判据：<b>第 0 类 CSS 文本级</b>（旧快照内联段 <b>==</b> <code>group.css</code> 逐字节，31078 B / <code>ea71fd30</code>；外加<b>重组互补证据</b> —— 把新件那 42 B 换回原 31095 B 段 → 与旧快照<b>逐字节相等（110833 B）</b>，证明「只换了这一处」）；<b>计算样式</b>（两侧各等 <code>readyState=complete</code> 且 <code>styleSheets</code> 连续两次采样稳定后，按文档序枚举 <code>body *</code>，固定 <b>92 个属性</b>的 <code>getComputedStyle</code> 字符串值）—— <b>87 个元素 × 92 属性 = 8004 个值逐行 diff 0 行</b>；<b>反向证据</b>（防「外链没加载，两个空页面也算一致」）—— 新侧必须有一张 <code>href</code> 以 <code>/group.css</code> 结尾、<code>cssRules.length &gt; 0</code> 的样式表（实测 <b>233 条规则</b>），两侧 CSSOM 拼接文本 <b>24425 字符、md5 同为 <code>21b3864b</code></b>，线级还断言<b>新侧真取过 <code>/group.css</code>、旧侧一次都没取</b>。原有各类全绿：JS 文本级 5/5、标识符 9/9、启动 4/4、轮询 1/1、动作序 13/13、函数级 7 例线级逐字节、两侧 JS 错误 0/0 —— 逐条 diff 合计 <b>0</b>，<code>EQUIV_SENTINEL_OK</code>；<code>--runs 3</code> 三次全 OK 且归一端口后<b>三份输出逐字节相同</b>（md5 <code>74d68140dfe3d8e26a04ec1ca03b15c0</code>，8184 B）→ <code>EQUIV_DETERMINISM_OK</code>。<br>
      ③ <b>3.2 基线重算</b>：登记件数 63 → <b>64</b>（新增 L4 <code>group.css</code>）、层间 import 边 <b>52</b>（不变 —— 前端静态件之间没有 import 边）。部署表 lobster 段受管件 19 → <b>20</b>（<code>ui/group.css</code>，<code>services=[]</code>，静态件免重启）。<br>
      ④ <b>已知副作用（不修，只记账）</b>：<code>agent-team/task_sys/patch_group_html.py</code>（一次性历史补丁脚本，无任何测试/服务引用它）在 <code>:208</code> 断言 <code>html.count("&lt;/style&gt;") == 1</code>、<code>:214</code> 拿 <code>&lt;/style&gt;</code> 当注入锚点 —— 本刀抽走后该锚点失效，它会<b>显式断言失败</b>（不是静默改错），故本刀不动它。<br>
      <b>后续闭合（刀20，阶段3）</b>：该件已<b>取证零调用方后物理删除</b>（件数 68→66），这条历史到此结清 —— 本节保留原文只为留痕，其中"本刀不动它"指的是刀12 当时的处置。<br>
      上一轮（v7）：① 阶段 2 <b>刀 10 · 前端运行真值收口</b> —— 判真值判出一个<b>假绿灯</b>：<code>deploy.py --status</code> 报四台<b>漂移 0</b>，但那<i>只覆盖部署表里的件</i>，<code>ui/</code> <b>静态资产从来不在表里</b>，于是前端在龙虾机上被直改（<code>#addagent</code> 加人对话框 / <code>#477</code> 图标 / <code>md.js</code> LaTeX 归一化）<b>整整两天对源码仓不可见</b>（源码仓 <code>group.html</code> 109101 B / <code>7f75ddb8</code> ≠ 线上 112774 B / <code>c7ff8b4e</code>；逐行 diff 证明<b>线上是严格超集</b> → 线上为真值）。处置：以线上字节<b>逐字节回写</b>源码仓 4 件（<code>group.html</code> + <code>md.js</code>/<code>tex.js</code>/<code>codeblock.js</code>），再把 <code>ui/</code> 五件<b>补登进 lobster 部署段</b>（<code>services=[]</code> —— <code>chat_svc._send_file</code> 每请求现读磁盘，静态件免重启）→ 受管件 14 → 19。<b>顺序不可反</b>：先回写再拆分；反了就是拿陈旧源码去部署，把线上多出来的功能<b>就地回退</b>。<br>
      ② 阶段 2 <b>刀 11 · 群聊前端「传输客户端」抽出</b>（前端拆三件套第一刀）—— 新件 <code>agent-team/ui/group_transport.js</code>（1983 B，<b>5 段纯拼接</b>：<code>API</code> 常量 + <code>api()</code> + <code>TSVC</code>/<code>TGROUP_DEFAULT</code> + <code>tApi()</code>；<b>无 IIFE、无 <code>type=module</code>、无 <code>"use strict"</code></b> —— 原内联脚本里那句 <code>"use strict"</code> 落在语句之后属<b>无效指令</b>，页面实际<b>非严格模式</b>，新件加严格模式反而会改语义）；<code>group.html</code> 删 5 段及各自紧随的空行、加 1 行 <code>&lt;script src="/group_transport.js"&gt;</code>（在内联脚本之前），112774 → <b>110833 B</b>。剩「壳 + 业务视图」待后续刀继续拆。<br>
      ③ 等价证据 <code>tools/equivalence/group_frontend_equiv.py</code>（无头 Edge + 手写 RFC6455 的 CDP，纯 stdlib；旧侧 = 开工前快照 <code>_old/group_old_k11.html</code>，装置开头<b>硬校验「旧侧 md5 前缀 = c7ff8b4e 且新旧 md5 不同」</b>防自比；<code>TSVC</code> 硬编码端点靠 CDP <code>Fetch</code> 域拦截 + <code>fulfillRequest</code> 合成应答，被测代码一字未动）：文本级 <b>5/5 逐段 <code>==</code></b>、标识符探针 <b>9/9</b>、启动轨迹 <b>4/4</b>、轮询轨迹 <b>1/1</b>、动作序轨迹 <b>13/13</b>、函数级 <b>7 例线级逐字节</b>、两侧 JS 错误 <b>0/0</b> —— 逐条 diff 合计 <b>0</b>（归一化只命中 <code>Referer</code>/端口/<code>next=</code>，「时间戳类规则一次都没触发」）。线上 9984 逐字节对照：served <code>group.html</code> <code>e5c46769</code> == 部署件、<code>/group_transport.js</code> 200 / 1983 B == 部署件；真机冒烟脚本序 <code>md·tex·hl·codeblock·group_transport·inline</code>、<code>api</code>/<code>tApi</code> 均 <code>function</code>；红点集合改前 = 改后 = <code>{blackboard_view, chat_svc, rt_remind, teamd}</code>（零 <code>.py</code> 改动，逻辑与实跑双证）。<br>
      ④ <b>3.2 基线重算</b>：登记件数 62 → <b>63</b>（新增 L4 <code>group_transport.js</code>）、层间 import 边 <b>52</b>（不变 —— 前端件之间没有 import 边）。<br>
      上上轮（v6）：① 阶段 2 <b>刀 9</b> —— <code>a2a_edge.py</code>（9920 A2A 端点）的 <b>HTTP 面</b>（应答出口 <code>_send_json</code>、两个 <code>do_*</code> 的路径分派、<code>_read_body</code>）收进 L1 <code>web_gateway.py</code>：<code>A2AHandler</code> 改继承 <code>GW.handler_class()</code>，路由改 <code>@GW.route</code> 登记表（<code>/healthz</code>、<code>/agents</code> 精确 + <code>/a2a</code> 的 GET/POST 前缀 + <code>@GW.not_found</code>），请求体改 <code>@GW.pre</code> 读一次并缓存；响应形态三件套（<code>server_version="a2a_svc/&lt;VERSION&gt;"</code> / <code>sys_version=""</code> / HTTP/1.1）在子类覆盖。<b>组合根 <code>a2a_svc.py</code> 一个字节未动</b>（已拍板：这一刀动的是 a2a_edge），注入路由协议靠把 <code>req.svc</code> 设为 <code>A2AHandler</code> 实例本身保住。<br>
      ② 等价证据 <code>tools/equivalence/a2a_edge_equiv.py</code> <b>46/46</b> 逐例一致（原 23 例一字未改；新增 23 例 = 路径判据边角 12 + 带体 6（含 300KB &gt; MAX_BODY_BYTES 的超大体）+ <b>keep-alive 同连接裸字节</b> 5）；另做<b>独立复核</b>：以本刀开工前快照 <code>_old/a2a_edge_old_k9.py</code> 为旧侧、直接对照<b>改前 vs 改后 46/46</b>（不复用装置旧侧）；线上四台逐字节对照（13 条只读探针 × 4 节点 + keep-alive 面）<b>0 处不一致</b>，<code>deploy.py --status</code> 漂移 0、健康 <b>11/11</b>。<br>
      ③ <b>3.2 基线重算</b>：登记件数 <b>62</b>（不变）、层间 import 边 51 → <b>52</b> —— <code>a2a_edge</code> 自本刀起 import L1 <code>web_gateway</code>（同层，方向合法），+1 条边。<br>
      上一轮（v5）：① 阶段 2 <b>刀 8a·8b</b> —— <code>chat_svc.py</code>（9980 / 9984 对话台）的<b>应答出口</b>、<b>非 GET 方法分派</b>（8a）与 <b>GET/HEAD 整张路由表 + 前端页服务</b>（8b）全部收进 L1 <code>web_gateway.py</code>；
      等价证据 <code>tools/equivalence/chat_svc_http_equiv.py</code> <b>311/311</b> 逐字节一致（状态码 + 状态原因 + <b>有序</b>响应头 + 归一后正文），四台的 L1 已落（<code>deploy.py --status</code> 漂移 0、健康 11/11）。
      ② <b>刀 8c</b> —— 等价装置 FakeDB 的形状按生产契约真实化（<code>set_session_meta</code> / <code>memo_get</code> / <code>memo_write</code> / <code>list_messages</code> / <code>todos_purge</code>）：
      311 例<b>不再有任何一例走 500 兜底</b>（500 例数 19 → <b>0</b>，2xx 126 → 143），生产代码一字未动；同步更正 <code>_old/chat_svc_old.py</code> 的来源登记（实为源码仓 rev 1264482 逐字节副本）。
      ③ <b>3.2 基线重算</b>：登记件数 53 → <b>62</b>、层间 import 边 17 → <b>51</b> —— 底座件被提为唯一出处后调用点各 import 一次，是解耦的账单，不是耦合增加。<br>
      上一轮（v4）：阶段 2 刀7c —— L1 <code>web_gateway.py</code> 的 JSON 信封函数<b>提升为公开面</b>
      <code>envelope()</code>（旧私有名 <code>_envelope</code> 保留为同一对象的兼容别名），
      唯一跨件私有调用点 <code>blackboard_svc.py</code> 切公开名 —— 判据 6「契约先于实现」在 L1 上闭合。
      等价证据：<code>tools/equivalence/gateway_envelope_equiv.py</code> 41/41 逐例一致（含 <code>_envelope is envelope</code> 别名同一对象断言）+
      <code>blackboard_svc_http_equiv.py</code> 155/155 一致。<br>
      上一轮（v3）：阶段 2 刀 2 —— 引入<b>组合根层 C</b>（进程入口装配）；<code>a2a_svc.py</code> 改为组合根，
      A2A 端点实现下沉新件 L1 <code>a2a_edge.py</code>（只声明能力端口，不 import 上层），
      <code>/ctx-usage</code> 的响应构造随之上移到装配侧，L1→L2 的反向依赖改为<b>引导期注入</b>；
      依赖方向的<b>已声明例外清零</b>。等价证据：<code>tools/equivalence/a2a_edge_equiv.py</code> 23/23 字节级一致。<br>
      上一轮（v2）：阶段 1 刀 1+2 —— 新增 L2 件 <code>agent_usage.py</code>，把 a2a_svc 与 ctx_watermark 里
      两份重复的「水位读取 / 窗口判定 / 会话头」合成<b>单一权威实现</b>（原两处窗口分母 1000000 与 200000 的分歧随之消失，
      统一取「部署生效口径」）；门禁新增<b>已声明例外</b>机制（8.5，可审计、带到期条件）；<br>
      首版（v1）：登记 53 件、17 条层间依赖、16 项端口；新增层归属表与四条门禁（<b>零业务码改动</b>）；
      并把「成员入网接口契约（草案 v0）」纳入本次评审（原属阶段 5，因它是通行层契约的成员，提前到阶段 0 定形状）<br>
      本文件由 <code>tools/gen_ledger_doc.py</code> 从 <code>tools/hacs_layers.json</code> 生成
    </div>
  </div>
</header>
<div class="wrap">
"""
HTML_FOOT = """
<footer>
  HACS 分层台账 · v12 · 2026-09-25 首版 / 2026-09-27 阶段 2–4 全闭 · 阶段 5 第三刀（刀27 · 名册档案化 + 逐字节自证）·
  规划文档：<code>docs/HACS-分层规划-handoff.html</code> ·
  机器可读孪生体：<code>tools/hacs_layers.json</code> · 门禁：<code>tools/check_layers.py</code> ·
  生成器：<code>tools/gen_ledger_doc.py</code><br>
  台账随件变化，正文只记口径与「凭据在哪个路径」，不含任何真实口令 / 令牌 / 连接串。
</footer>
</div>
</body>
</html>
"""

html = HTML_HEAD + '\n'.join(secs[:6]) + '\n' + SVG + '\n' + '\n'.join(secs[6:]) + HTML_FOOT
io.open(OUT, 'w', encoding='utf-8', newline='\n').write(html)
n = html.count('<h2>')
print('已生成:', OUT)
print('字节:', len(html.encode('utf-8')), '| 行数:', html.count('\n') + 1)
print('h2 章节数:', n, '| table 数:', html.count('<table>'), '| svg 数:', html.count('<svg'))
