"""用可追溯的本地规则整理单页专家简介，不生成原文没有的资历。"""

import re

from .ooxml import compact


# 长词优先，避免把副主任医师截成主任医师，把博士后截成博士。
CLINICAL = re.compile(r'副主任中医师|主任中医师|副主任医师|主任医师|主治医师|住院医师|副主任护师|主任护师|主管护师')
ACADEMIC = re.compile(
    r'(?:临床医学|临床|医学|肿瘤学)?(?:博士后(?:研究员)?|(?:博士|硕士)(?:研究生)?(?:生)?(?:导师|导)?|研究生导师|研究生导|研究生|硕导|博导)'
    r'|(?:临床)?(?:副)?教授|(?:副)?研究员|访问学者')
SOCIETY = re.compile(r'协会|学会|分会|委员会|专委会|专委|学组|理事|编委|编辑委员会|协作组|促进会|联盟')
SOCIAL_ROLE = re.compile(r'委员|常委|主委|理事|会长|组长|秘书|编委|常务|顾问')
DEPARTMENT = re.compile(r'科|病区|病房|院区|中心|EICU|ICU|院长|院士|MDT', re.I)
HEADINGS = {'学术兼职', '学术任职', '社会兼职', '专业特长', '课题', '科研项目', '研究方向', '获奖', '荣誉'}
MEETING_ROLE_PREFIX = re.compile(r'^(?:(?:大会|会议|环节)?(?:讨论嘉宾|讨论专家|讲者|主持人?|主席|讨论))(?:[:：]\s*)?')
MEETING_TOPIC_PREFIX = re.compile(r'^(?:(?:大会|会议|环节)?(?:讲者|主持人?|主席|讨论))主题[:：]?')
MAX_BIO_LINES = 12


def clean(value):
    value = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', value)
    # 只去掉列表序号，保留履历中的年份与数量。
    value = re.sub(r'^\s*\d+[.、)]\s*', '', value)
    return re.sub(r'^[\s\-•·◆▪]+', '', value).strip(' \t，,、；;。:：')


def unique(values):
    result = []
    for value in values:
        value = clean(value)
        if value and compact(value) not in {compact(x) for x in result}:
            result.append(value)
    return result


def split_other_line(value):
    """从长段原文中提取完整短句，著作信息按书名保留可核对的角色。"""
    books = re.findall(r'《[^》]+》', value)
    if books and re.search(r'著作|编写|主编|出版', value):
        result = []
        for index, book in enumerate(books):
            if index == 0 and '副主编' in value:
                result.append(book + '副主编')
            elif index == 0 and re.search(r'(?<!副)主编', value):
                result.append(book + '主编')
            else:
                result.append('参与' + book + '著作编写')
        return result
    # 普通个人介绍保留逗号和顿号，只按完整句子或分号拆分。
    parts = [clean(part) for part in re.split(r'[。；;]+', value) if clean(part)]
    result = []
    for part in parts:
        # 常见长句只删除冗余连接词，保留专业方向、疾病名称、项目层级与数量。
        part = part.replace('主要从事', '从事').replace('的临床与科研工作', '临床科研')
        part = part.replace('等消化内科常见肿瘤的诊治', '诊治')
        part = part.replace('胸部肿瘤的放射治疗相关的', '胸部肿瘤放疗')
        part = part.replace('急危重症尤其是心脏重症的诊治，以及', '心脏重症诊治及')
        part = part.replace('的实施与质控', '')
        part = part.replace('主持山东省科技卫生项目青年基金', '主持省科技卫生青年基金')
        part = part.replace('，中国博士后科研基金', '、博士后科研基金')
        result.append(part)
    return result


def bio_source_line(value, name):
    """清除会议角色标签，保留同一行中真实的个人简介内容。"""
    value = clean(value)
    if MEETING_TOPIC_PREFIX.match(value):
        return ''
    match = MEETING_ROLE_PREFIX.match(value)
    if match:
        value = clean(value[match.end():])
        if name:
            value = clean(re.sub(r'^' + re.escape(name), '', value))
    return value


def split_social_line(value):
    """按资料中的并列标点拆开社会任职，避免整段任职占用一行。"""
    parts = [clean(part) for part in re.split(r'[，,、；;。]+', value) if clean(part)]
    return parts or [clean(value)]


def summarize_bio(expert):
    """整理最多十二条简介，优先临床信息、学校履历和原文社会任职。"""
    source = [line for value in expert.get('bio', [])
              for line in [bio_source_line(value, expert.get('name', ''))] if line]
    if source == ['简介待补充']:
        lines = ['简介待补充']
        return {'lines': lines, 'clinical': '', 'academic': '', 'social': [], 'other': [],
                'placeholders': [], 'source': source, 'missing': ['简介']}
    # 修复文本框里人为拆开的委员会名称，避免输出半条任职。
    joined = []
    for line in source:
        if joined and joined[-1].endswith('委') and line.startswith('会'):
            joined[-1] += line
        else:
            joined.append(line)
    hospital = expert.get('hospital') or expert.get('display_hospital', '')
    # 医院名称紧接协会或学会时属于组织名称，不能当作专家工作医院。
    workplace = bool(hospital and any(re.search(re.escape(hospital) + r'(?!协会|学会|分会|委员会)', line)
                                      for line in joined))
    if SOCIETY.search(hospital) or not workplace:
        hospital = expert.get('display_hospital', '')
    # 先按句号和分号分段，避免同一文本框中的医院职称被后续学术任职干扰。
    units = [part for line in joined for part in split_other_line(line)]
    clinical, titles, academic, social, other = [], [], [], [], []
    for line in units:
        if clean(line).rstrip(':：') in HEADINGS:
            continue
        pending = [line]
        if SOCIETY.search(line) or re.search(r'委员|常委|主委', line):
            # 同一段中的多项任职按逗号、顿号和分号拆开，每项占一行参与排序。
            pending = []
            for part in split_social_line(line):
                if SOCIAL_ROLE.search(part) and not re.search(r'^师从|发表|主持.*基金|擅长', part):
                    social.append(part)
                elif part:
                    pending.append(part)
        for item in pending:
            if re.search(r'^(主要|长期从事|从事|擅长|熟练|研究方向|专注|致力|对|临床|临证|发表|主持|承担|参研|专业特长|学术兼职|学术任职|社会兼职|科研|师从|齐鲁人才|济南市劳动)|^在.*(?:发表|完成|出版)|^曾.*(?:荣获|获奖)|著作|编写|出版', item):
                if not re.search(r'^(学术兼职|学术任职|社会兼职|专业特长)[:：]?$', item):
                    other.append(item)
                continue
            if re.search(r'大学|学院', item) and re.search(r'研究所|实验室|访问学者', item):
                academic.append(item)
                continue
            # 临床职称和教授职称并入首行，学历及导师信息单独成行。
            titles.extend(CLINICAL.findall(item))
            titles.extend(re.findall(r'副教授|(?<!副)教授', item))
            rest = clean(CLINICAL.sub('', item).replace('中共党员', ''))
            rest = clean(re.sub(r'副教授|(?<!副)教授', '', rest))
            if not rest:
                continue
            pieces = [clean(part) for part in re.split(r'[，,、]+', rest) if clean(part)]
            for piece in pieces:
                match = ACADEMIC.search(piece)
                if match:
                    tail = clean(piece[match.end():])
                    if match.start() == 0 and tail and DEPARTMENT.search(tail) and not ACADEMIC.search(tail):
                        academic.append(match.group())
                        clinical.append(tail)
                        continue
                    prefix = clean(piece[:match.start()])
                    # 同一行兼有医院科室和导师头衔时，在头衔起点分开。
                    if prefix and ('医院' in prefix or DEPARTMENT.search(prefix)) and not re.search(r'大学|学院', prefix.replace(hospital, '')):
                        clinical.append(prefix)
                        academic.append(piece[match.start():])
                    else:
                        academic.append(piece)
                elif hospital and piece == hospital:
                    continue
                elif '医院' in piece or DEPARTMENT.search(piece):
                    # 医院MDT专长与职务保留在原文中，首行优先主要科室。
                    if not re.search(r'MDT|协作|专家', piece):
                        clinical.append(piece)
                else:
                    other.append(piece)
    clinical = unique(clinical)
    primary = next((x for x in clinical if hospital and x.startswith(hospital) and x != hospital), '')
    departments = [x for x in clinical if x != primary and '医院' not in x]
    first = primary or hospital
    if departments and not primary:
        department = departments[0]
        if department not in first and not re.match(r'^(任|历任|曾任|牵头|组建)', department):
            first += department
    for title in unique(titles):
        if title not in first:
            first += (('，' if not any(existing in first for existing in unique(titles)) else '、') if first else '') + title
    # 学历与导师优先于其他学校任职，保留限定词及学校名称，不推断导师层级。
    academic = unique(academic)
    academic.sort(key=lambda x: (0 if re.search(r'导师|硕导|博导|生导', x) else 1 if re.search(r'博士|硕士', x) else 2))
    second = '，'.join(academic[:2])
    # 其他领域职称和个人简介按原资料出现顺序展示，避免自动排序改变作者表达顺序。
    social = unique(social)
    other = unique(other)
    missing = []
    if not first:
        missing.append('医院及临床职务')
    if not second:
        missing.append('学历或学校相关履历')
    if not any(DEPARTMENT.search(x.replace(hospital, '')) for x in clinical):
        missing.append('科室')
    if not titles and not any(re.search(r'主任|院长|医师', x) for x in clinical):
        missing.append('临床职务职称')
    # 没有学校履历时直接承接社会任职，其余原文用于补足，资料不足时不显示占位文字。
    lines = ([first] if first else []) + ([second] if second else [])
    for value in social + other:
        if value and compact(value) not in {compact(x) for x in lines}:
            lines.append(value)
        if len(lines) == MAX_BIO_LINES:
            break
    selected_social = [x for x in lines if x in social]
    return {'lines': lines[:MAX_BIO_LINES], 'clinical': first, 'academic': second, 'social': social, 'other': other,
            'selected_social': selected_social, 'placeholders': [],
            'source': list(expert.get('bio', [])), 'missing': missing}
