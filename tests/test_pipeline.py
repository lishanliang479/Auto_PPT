"""真实样本与边界条件测试，验证匹配、保真复制和失败处理。"""

import copy
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from autoppt.analyze import analyze, analyze_template, classify_event, discover, filename_name, image_title, names_in, parse_agenda, read_expert
from autoppt.bio import summarize_bio
from autoppt.generate import display_name, find_shape, fit_lines, fit_meeting_title, generate, page_plan, photo_frame_shape, portrait_bytes, set_text, text_box, validate_model
from autoppt.ooxml import NS, compact, encoded, read_deck, read_package, unpack_experts, xml
from autoppt.poster import clean_name, organizer_from_logo, render_poster, unpack_portraits
from autoppt.server import Handler, TOKEN, ThreadingHTTPServer, apply_edits, apply_poster_edits, ppt_output_filename

ROOT = Path(__file__).resolve().parent.parent


class RuleTests(unittest.TestCase):
    def test_overflowing_text_wraps_and_shrinks_inside_shape(self):
        # 普通讲题和名单超过文本框容量时启用换行，并在最低字号范围内缩放。
        node = xml(b'''<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
            <p:txBody><a:bodyPr lIns="0" rIns="0" tIns="0" bIns="0" wrap="none"/><a:lstStyle/>
            <a:p><a:r><a:rPr sz="4800"><a:ea typeface="Microsoft YaHei"/></a:rPr>
            <a:t>old</a:t></a:r></a:p></p:txBody></p:sp>''')
        shape = {'bbox': (0, 0, 5200000, 1300000), 'font': 48}
        lines = ['因病制宜，阶梯镇痛——中国骨关节炎诊疗指南（2024版）解读']
        overflow, size = fit_lines(node, shape, lines, minimum=18)
        self.assertFalse(overflow)
        self.assertLess(size, 48)
        self.assertEqual(node.find('p:txBody/a:bodyPr', NS).get('wrap'), 'square')

    def test_long_meeting_title_is_kept_on_one_line(self):
        # 长会议名称按文本框宽度缩小字号，并关闭自动换行。
        node = xml(b'''<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
            <p:txBody><a:bodyPr lIns="0" rIns="0" tIns="0" bIns="0"/><a:lstStyle/>
            <a:p><a:r><a:rPr sz="2400"><a:ea typeface="Microsoft YaHei"/></a:rPr>
            <a:t>old</a:t></a:r></a:p></p:txBody></p:sp>''')
        shape = {'bbox': (0, 0, 8000000, 500000), 'font': 24}
        title = '汇智齐鲁，共护慢病全域慢病防治与综合管理学术研讨会'
        overflow, size = fit_meeting_title(node, shape, title)
        self.assertFalse(overflow)
        self.assertLess(size, 24)
        self.assertEqual(node.find('p:txBody/a:bodyPr', NS).get('wrap'), 'none')
        self.assertEqual(''.join(node.xpath('.//a:t/text()', namespaces=NS)), title)

    def test_very_long_meeting_title_wraps_inside_template_box(self):
        # 单行需要过小字号时恢复自动换行，并同时按文本框高度缩放。
        node = xml(b'''<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
            <p:txBody><a:bodyPr lIns="0" rIns="0" tIns="0" bIns="0"/><a:lstStyle/>
            <a:p><a:r><a:rPr sz="2400"><a:ea typeface="Microsoft YaHei"/></a:rPr>
            <a:t>old</a:t></a:r></a:p></p:txBody></p:sp>''')
        shape = {'bbox': (0, 0, 4200000, 900000), 'font': 24}
        title = '汇智齐鲁，共护慢病全域慢病防治与综合管理学术研讨会暨慢性疾病规范化诊疗能力提升会议'
        overflow, size = fit_meeting_title(node, shape, title)
        self.assertFalse(overflow)
        self.assertLess(size, 24)
        self.assertEqual(node.find('p:txBody/a:bodyPr', NS).get('wrap'), 'square')
        self.assertEqual(''.join(node.xpath('.//a:t/text()', namespaces=NS)), title)

    def test_template_default_font_and_size_are_materialized(self):
        # 模板将字体和字号放在段落默认样式时，新文字仍需完整继承该板块格式。
        node = xml(b'''<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
            <p:txBody><a:bodyPr/><a:lstStyle><a:lvl1pPr><a:defRPr sz="2200" b="1">
            <a:ea typeface="Microsoft YaHei"/></a:defRPr></a:lvl1pPr></a:lstStyle>
            <a:p><a:pPr lvl="0"><a:defRPr sz="1800"><a:ea typeface="SimSun"/></a:defRPr></a:pPr>
            <a:r><a:rPr lang="zh-CN"/><a:t>old</a:t></a:r></a:p></p:txBody></p:sp>''')
        set_text(node, ['new'])
        props = node.find('.//a:r/a:rPr', NS)
        self.assertEqual(props.get('sz'), '1800')
        self.assertEqual(props.get('b'), '1')
        self.assertEqual(props.find('a:ea', NS).get('typeface'), 'SimSun')
        _, _, size, family = text_box(node, {'bbox': (0, 0, 2540000, 1270000), 'font': 36})
        self.assertEqual(size, 18)
        self.assertEqual(family, 'SimSun')

    def test_two_character_name_has_one_space(self):
        # 两字姓名只调整展示形式，三字及以上姓名保持原样。
        self.assertEqual(display_name('田静'), '田 静')
        self.assertEqual(display_name('张培根'), '张培根')

    def test_ppt_picture_rotation_is_applied_to_portrait(self):
        # 专家PPT用对象旋转纠正横向原图时，生成头像应保持相同视觉方向。
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'rotated.pptx'
            original = Image.new('RGB', (400, 200), 'red')
            for x in range(200, 400):
                for y in range(200):
                    original.putpixel((x, y), (0, 0, 255))
            payload = io.BytesIO()
            original.save(payload, 'PNG')
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('ppt/media/image1.png', payload.getvalue())
            expert = {'path': str(source), 'photo': 'ppt/media/image1.png',
                      'photos': [{'image': 'ppt/media/image1.png', 'rotation': 90}]}
            with Image.open(io.BytesIO(portrait_bytes(expert, .5))) as result:
                top = result.getpixel((result.width // 2, result.height // 6))
                bottom = result.getpixel((result.width // 2, result.height * 5 // 6))
                self.assertGreater(top[0], top[2])
                self.assertGreater(bottom[2], bottom[0])

    def test_missing_person_after_hospital(self):
        self.assertEqual(names_in('李 磊 教授 单县中心医院 朱 帅 教授 山东大学齐鲁医院', ['李磊']), ['李磊', '朱帅'])
        self.assertEqual(names_in('杨秀婷教授山东大学齐鲁医院崔景利教授山东省肿瘤医院'), ['杨秀婷', '崔景利'])

    def test_adjacent_names_without_separator(self):
        self.assertEqual(names_in('杜忠海教授刘新会教授杨金霞教授赵倩教授祝守慧教授'), ['杜忠海', '刘新会', '杨金霞', '赵倩', '祝守慧'])

    def test_filename_name_ignores_date_and_role(self):
        # 日期和角色紧贴姓名时，仍应稳定提取真实专家姓名。
        cases = {
            '000_10-0904孟洪正 讨论.docx': '孟洪正',
            '001_1-0904主持  孙华强.docx': '孙华强',
            '002_2-0904吴昊讲者.docx': '吴昊',
            '003_3-0904张培根讨论.docx': '张培根',
            '005_5-0904于鑫 讲者.docx': '于鑫',
            '006_6-0904李军讨论.wps': '李军',
            '007_7-0904杨楠楠 讨论嘉宾.docx': '杨楠楠',
            '009_9-0904刘欢讨论.docx': '刘欢',
        }
        for source, expected in cases.items():
            self.assertEqual(filename_name(source), expected)

    def test_roundtable_names_are_discussion_events(self):
        # 圆桌及对话类名称属于多人讨论环节，后续人物页统一使用讨论嘉宾角色。
        for title in ('圆桌对话', '圆桌交流', '专家对话', '互动交流'):
            self.assertEqual(classify_event(title), 'discussion')

    def test_long_social_paragraph_splits_into_separate_lines(self):
        # 逗号、顿号和分号连接的任职应拆为独立条目，避免整段占用一行。
        expert = {'name': '李军', 'hospital': '', 'bio': [
            '讨论',
            '骨伤一科（关节运动医学科）主任医师。',
            '中国中西医结合学会骨科微创专业委员会委员，中国中西医结合学会疼痛专业委员会委员、'
            '中华中医药学会筋膜学协同创新共同体委员；中国中医药研究促进会骨质疏松分会理事，'
            '中国民族医药学会骨伤分会理事，山东中医药学会骨伤分会常委委员。',
        ]}
        summary = summarize_bio(expert)
        self.assertEqual(len(summary['lines']), 7)
        self.assertEqual(summary['lines'][0], '骨伤一科（关节运动医学科），主任医师')
        self.assertIn('中国中西医结合学会骨科微创专业委员会委员', summary['lines'])
        self.assertTrue(all('，中国' not in line and '、中华' not in line for line in summary['lines']))

    def test_meeting_role_removed_and_prose_keeps_commas(self):
        # 会议角色不进入个人简介，普通叙述中的逗号继续属于同一句话。
        summary = summarize_bio({'name': '张培根', 'hospital': '甲医院', 'bio': [
            '讨论嘉宾', '甲医院副主任医师',
            '长期从事老年慢性疾病的诊治工作，对老年多发病，常见病的防治有较深的造诣。']})
        self.assertNotIn('讨论嘉宾', summary['lines'])
        self.assertIn('长期从事老年慢性疾病的诊治工作，对老年多发病，常见病的防治有较深的造诣', summary['lines'])
        self.assertFalse(any('资料未提供' in line or '待补充' in line for line in summary['lines']))

    def test_mixed_paragraph_keeps_hospital_and_title_first(self):
        # 同一长段落含医院、职称和学会任职时，首行仍由医院及临床职称组成。
        summary = summarize_bio({'name': '刘江', 'hospital': '山东省第二人民医院', 'bio': [
            '山东省第二人民医院关节外科二区主任，副主任医师，医学博士，博士后。'
            '山东省医学会骨科分会委员。擅长关节置换术。']})
        self.assertEqual(summary['lines'][0], '山东省第二人民医院关节外科二区主任，副主任医师')
        self.assertEqual(summary['lines'][1], '医学博士，博士后')

    def test_professor_and_clinical_title_share_first_line(self):
        # 教授与主任医师均为专家职称，应与医院合并显示在首行。
        summary = summarize_bio({'name': '刁维珍', 'hospital': '山东中医药大学附属医院', 'bio': [
            '山东中医药大学附属医院主任医师、教授。任康复科主任。临证四十余年，擅长康复治疗。']})
        self.assertEqual(summary['lines'][0], '山东中医药大学附属医院，主任医师、教授')
        self.assertIn('临证四十余年，擅长康复治疗', summary['lines'])

    def test_picture_fill_and_docx_decorations(self):
        # 真实问题样本同时覆盖形状图片填充和Word装饰线，确保自动选择可用头像。
        profiles = ROOT / 'input' / '9.15百利天恒 线上会议' / '专家简介'
        filled_shape = profiles / '1-程金刚简介.pptx'
        decorated_docx = profiles / '9-贾亦斌简介.docx'
        if not filled_shape.exists() or not decorated_docx.exists():
            self.skipTest('9.15头像识别样本未放入项目')
        ppt_expert = read_expert(filled_shape)
        docx_expert = read_expert(decorated_docx)
        self.assertEqual(ppt_expert['photos'][0]['image'], 'ppt/media/image2.jpeg')
        self.assertGreater(ppt_expert['photos'][0]['score'], ppt_expert['photos'][1]['score'])
        self.assertEqual(docx_expert['photos'][0]['image'], 'word/media/image2.jpeg')
        self.assertTrue(docx_expert['photo_confirmed'])

    def test_agenda_title_from_header_image(self):
        # 日程标题转成顶部横幅图片时，使用本地OCR恢复完整会议名称。
        source = ROOT / 'input' / '9.21康弘' / '9.21康弘日程.pptx'
        if not source.exists():
            self.skipTest('9.21图片标题样本未放入项目')
        agenda = parse_agenda(source, [])
        self.assertEqual(agenda['title'], '汇智齐鲁，共护慢病全域慢病防治与综合管理学术研讨会')

    def test_missing_ocr_dependency_is_reported(self):
        # 图片标题依赖缺失时继续允许生成，并在核对区说明会议名称为空的具体原因。
        deck = {'width': 100, 'height': 100, 'slides': [{'shapes': [
            {'image': 'ppt/media/title.png', 'bbox': (0, 0, 100, 20)}]}]}
        warnings = []
        with patch.dict('sys.modules', {'rapidocr': None}):
            self.assertEqual(image_title(ROOT / 'missing.pptx', deck, warnings), '')
        self.assertTrue(any(item['code'] == 'agenda_title_ocr_unavailable' for item in warnings))

    def test_generated_filename_uses_meeting_title_and_time(self):
        # 下载文件名包含会议名称、日期及首尾日程时间，并清除Windows禁用字符。
        model = {'agenda': {'title': '汇智齐鲁，共护慢病', 'date': '2026年9月21日', 'events': [
            {'time': '19:00-19:10'}, {'time': '20:20-20:30'}]}}
        self.assertEqual(ppt_output_filename(model),
                         '汇智齐鲁，共护慢病2026年9月21日19时00分至20时30分.pptx')
        model['agenda']['title'] = '研讨会:A/B'
        self.assertNotRegex(ppt_output_filename(model), r'[<>:"/\\|?*]')

    def test_narrow_portrait_keeps_head(self):
        # 窄幅全身照填充较宽照片框时从顶部裁切，头部区域继续保留在输出中。
        source = ROOT / 'input' / '9.8费卡庞经理' / '9.8费卡专家简介' / '10-0908丁红光.docx'
        if not source.exists():
            self.skipTest('9.8窄幅头像样本未放入项目')
        expert = read_expert(source)
        data = portrait_bytes(expert, 2160000 / 2876400)
        with Image.open(io.BytesIO(data)) as output:
            self.assertEqual(output.size, (451, 600))
            # 原图头部位于顶部中央，输出相同位置应保留肤色像素。
            red, green, blue = output.convert('RGB').getpixel((225, 70))
            self.assertGreater(red, green)
            self.assertGreater(green, blue)

    def test_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'bad.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('../outside.docx', b'bad')
            with self.assertRaisesRegex(ValueError, '不安全'):
                unpack_experts(source, Path(temp) / 'safe')
            self.assertFalse((Path(temp) / 'outside.docx').exists())

    def test_poster_portrait_zip_and_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'portraits.zip'
            image = io.BytesIO()
            Image.new('RGB', (40, 60), 'white').save(image, 'PNG')
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('头像/01_张三教授.png', image.getvalue())
            portraits = unpack_portraits([source], root / 'expanded')
            self.assertEqual(len(portraits), 1)
            self.assertEqual(clean_name('01_张三教授.png'), '张三')

    def test_poster_logo_unit_mapping(self):
        # 标志简称在日程左上角被识别后，可稳定映射为正式主办单位名称。
        items = [{'text': 'SSPCA', 'bbox': [10, 10, 80, 30]}]
        self.assertEqual(organizer_from_logo(items, 1000, 1600), '山东省亚健康防治协会')

    def test_poster_edits_keep_server_paths(self):
        original = {'agenda': {'title': '甲会议', 'date': '', 'meeting_time': '', 'meeting_code': '',
                               'venue': '', 'organizer': '', 'chairs': ['张三'], 'events': [
                                   {'time': '10:00-10:20', 'kind': 'talk', 'title': '甲',
                                    'people': ['张三'], 'hosts': []}]},
                    'experts': [{'name': '张三', 'hospital': '甲医院', 'display_title': '教授',
                                 'role': 'speaker', 'path': 'C:/safe/photo.png'}]}
        incoming = copy.deepcopy(original)
        incoming['experts'][0].update(name='李四', path='C:/unsafe/replaced.png')
        updated = apply_poster_edits(original, incoming)
        self.assertEqual(updated['experts'][0]['path'], 'C:/safe/photo.png')
        self.assertEqual(updated['agenda']['events'][0]['people'], ['李四'])

    def test_render_poster_png(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            template, portrait, output = root / 'template.jpg', root / '张三.png', root / 'poster.png'
            Image.new('RGB', (900, 1800), '#f4d9ef').save(template)
            Image.new('RGB', (300, 420), '#dbe8f4').save(portrait)
            model = {'template': {'path': str(template)}, 'agenda': {
                'title': '测试会议', 'date': '2026年9月20日', 'meeting_time': '10:00-10:20',
                'meeting_code': '123-456-789', 'organizer': '测试单位', 'events': [
                    {'time': '10:00-10:20', 'kind': 'talk', 'title': '测试讲题',
                     'people': ['张三'], 'hosts': []}]},
                'experts': [{'name': '张三', 'hospital': '甲医院', 'display_title': '教授',
                             'role': 'speaker', 'path': str(portrait)}]}
            report = render_poster(model, output)
            self.assertTrue(output.exists())
            with Image.open(output) as image:
                self.assertEqual(image.format, 'PNG')
                self.assertEqual(image.size, (report['width'], report['height']))


class RecentTemplateTests(unittest.TestCase):
    def test_taide_portraits_align_to_visible_rounded_frames(self):
        # 泰德模板的原照片对象与圆角框坐标不同，生成后必须统一对齐到可见框。
        base = ROOT / 'input' / '9.4泰德慢性及术后'
        if not base.exists():
            self.skipTest('泰德真实资料未放入项目')
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            model = analyze(base / '9.4泰德慢性及术后串场.pptx',
                            base / '9.4泰德慢性及术后日程.pptx',
                            [base / '9.4泰德慢性及术后专家简介.zip'], work / 'source')
            output = work / 'taide-align.pptx'
            report = generate(model, output)
            deck = read_deck(output)
            profile = {slide['number']: slide for slide in model['template']['slides']}
            for page, spec in zip(deck['slides'], report['pages']):
                if not spec.get('expert'):
                    continue
                source = profile[spec['template_slide']]
                photo_id = spec['fields'].get('photo')
                source_photo = next(shape for shape in source['shapes'] if shape['id'] == photo_id)
                frame = photo_frame_shape(source, source_photo)
                output_photo = next(shape for shape in page['shapes'] if shape['id'] == photo_id)
                self.assertIsNotNone(frame, spec['expert'])
                self.assertEqual(output_photo['bbox'], frame['bbox'], spec['expert'])

    def test_recent_template_layout_variants_are_recognized(self):
        # 覆盖日程表、无照片简介、多人讨论页和标题日期共框等近期模板结构。
        folder = ROOT / 'input' / 'ppt模板'
        if not folder.exists():
            self.skipTest('最近模板未放入项目')
        gynecology = analyze_template(folder / '康缘妇科桂在有你散发光彩串场.pptx')
        self.assertEqual(gynecology['slides'][1]['role'], 'agenda')
        self.assertEqual(gynecology['slides'][9]['role'], 'guest')
        self.assertEqual(gynecology['slides'][9]['fields']['bio'], '10')

        nutrition = analyze_template(folder / '9.8费卡庞经理串场.pptx')
        self.assertEqual(nutrition['slides'][3]['fields']['bio'], '6')
        self.assertNotIn('6', nutrition['slides'][3]['fields']['meeting'])

        orthopedics = analyze_template(folder / '康缘骨科”星耀医路 腰你同行”串场模板.pptx')
        self.assertEqual(orthopedics['slides'][1]['role'], 'agenda')
        self.assertEqual(orthopedics['slides'][6]['role'], 'speaker')
        self.assertGreaterEqual(len(orthopedics['slides'][16]['fields']['people']), 5)
        self.assertGreaterEqual(len(orthopedics['slides'][16]['fields']['people_photos']), 5)

        for path in folder.glob('*.pptx'):
            profile = analyze_template(path)
            cover = next(slide for slide in profile['slides'] if slide['role'] == 'cover')
            self.assertTrue(cover['fields'].get('title'), path.name)


@unittest.skipUnless((ROOT / 'input1').exists(), '真实资料未放入项目')
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.temp.name)
        cls.models = {}
        cls.hashes = {}
        for name in ('input', 'input1'):
            if name == 'input':
                # input目录包含多批历史资料，测试固定使用原有9月7日样例。
                template = ROOT / 'input' / '串场PPT模板.pptx'
                agenda = ROOT / 'input' / '9.7费卡华瑞日程.pptx'
                experts = [ROOT / 'input' / '专家简介.zip']
            else:
                template, agenda, experts = discover(ROOT / name)
            cls.models[name] = analyze(template, agenda, experts, cls.work / name)
            cls.hashes[template] = hashlib.sha256(template.read_bytes()).hexdigest()
        cls.output = cls.work / 'meeting.pptx'
        cls.report = generate(cls.models['input1'], cls.output)
        cls.deck = read_deck(cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_all_experts_and_sessions(self):
        model = self.models['input1']
        self.assertEqual(len(model['experts']), 16)
        self.assertEqual(model['agenda']['title'], '胸部肿瘤诊疗进展交流会')
        events = model['agenda']['events']
        self.assertEqual([e['kind'] for e in events], ['opening', 'talk', 'discussion', 'talk', 'discussion', 'summary'])
        self.assertEqual(events[1]['hosts'], ['宁方玲'])
        self.assertEqual(events[3]['hosts'], ['时圣彬'])
        self.assertEqual(model['agenda']['people']['宁方玲']['display_title'], '教授')
        self.assertEqual(events[4]['people'], ['杜忠海', '刘新会', '杨金霞', '赵倩', '祝守慧'])

    def test_generation_options_use_expected_defaults(self):
        # 主席简介与待核对模式默认开启，讨论话题页默认关闭。
        options = self.models['input1']['options']
        self.assertTrue(options['repeat_chairs'])
        self.assertFalse(options['include_topics'])
        self.assertTrue(options['draft'])
        self.assertNotIn('bio_font_size', options)

    def test_table_agenda_and_missing_expert(self):
        model = self.models['input']
        self.assertEqual(len(model['agenda']['events']), 6)
        self.assertEqual(model['agenda']['events'][4]['people'], ['李磊', '朱帅'])
        self.assertEqual(model['agenda']['people']['朱帅']['hospital'], '山东大学齐鲁医院德州医院')
        # 缺少简介时继续生成，并在校验提示中说明待补充人员。
        model = copy.deepcopy(model)
        model['options']['draft'] = False
        missing, warnings = validate_model(model)
        self.assertIn('朱帅', missing)
        self.assertTrue(any(item['code'] == 'missing_expert' for item in warnings))

    def test_paragraph_breaks(self):
        expert = next(e for e in self.models['input1']['experts'] if e['name'] == '尹贻波')
        self.assertTrue(any(line == '山东省医师协会肿瘤精准医疗医师分会委员' for line in expert['bio']))

    def test_table_job_pair(self):
        expert = next(e for e in self.models['input1']['experts'] if e['name'] == '毕经旺')
        self.assertIn('中国医药教育协会罕见病学专业委员会副主任委员', [compact(x) for x in expert['bio']])

    def test_filename_name_fallback(self):
        self.assertTrue(any(i['code'] == 'name_from_filename' and i['expert'] == '王慧君' for i in self.models['input1']['issues']))

    def test_zip_duplicate_inputs(self):
        template, agenda, experts = discover(ROOT / 'input1')
        model = analyze(template, agenda, experts + [ROOT / 'input1/8.13专家简介.zip'], self.work / 'zip')
        self.assertEqual(len(model['experts']), 16)

    def test_output_preserves_dimensions_and_original_parts(self):
        model = self.models['input1']
        self.assertEqual((self.deck['width'], self.deck['height']), (model['template']['width'], model['template']['height']))
        original = read_package(model['template']['path'])
        produced = read_package(self.output)
        for name, data in original.items():
            if name.startswith(('ppt/theme/', 'ppt/slideMasters/', 'ppt/slideLayouts/')) and name in produced:
                self.assertEqual(data, produced[name], name)

    def test_old_people_and_dates_removed(self):
        with zipfile.ZipFile(self.output) as archive:
            payload = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith('.xml'))
        for old in ('韩淑梅', '杨正强', '2026年07月21日', '李晓倩'):
            self.assertFalse(old in payload, f'输出仍包含旧内容：{old}')
        self.assertFalse(any('notesSlides/' in name for name in read_package(self.output)))

    def test_selected_bio_is_complete_and_single_page(self):
        model = self.models['input1']
        source_package = read_package(model['template']['path'])
        output_package = read_package(self.output)
        self.assertEqual(len(self.deck['slides']), len(page_plan(model)))
        for slide, spec in zip(self.deck['slides'], self.report['pages']):
            if not spec.get('expert'):
                continue
            self.assertEqual(spec['continuation'], 1)
            self.assertGreaterEqual(len(spec['selected_bio']), 1)
            self.assertLessEqual(len(spec['selected_bio']), 12)
            for line in spec['selected_bio']:
                self.assertIn(compact(line), compact(slide['text']))
            source = model['template']['slides'][spec['template_slide'] - 1]
            body = next(s for s in slide['shapes'] if s['id'] == source['fields']['bio'])
            self.assertEqual(body['lines'][0], spec['selected_bio'][0])
            # 专家简介正文沿用当前模板简介区域的字号。
            self.assertEqual(body['font'], source['shapes'][next(
                index for index, item in enumerate(source['shapes']) if item['id'] == source['fields']['bio'])]['font'])
            # 每段字号和行距均直接继承对应的模板段落。
            source_node = find_shape(xml(source_package[source['part']]), source['fields']['bio'])
            output_node = find_shape(xml(output_package[slide['part']]), source['fields']['bio'])
            source_paragraphs = [p for p in source_node.findall('.//a:p', NS)
                                 if p.xpath('.//a:t/text()', namespaces=NS)]
            output_paragraphs = output_node.findall('.//a:p', NS)
            for index, paragraph in enumerate(output_paragraphs):
                template_paragraph = source_paragraphs[min(index, len(source_paragraphs) - 1)]
                self.assertEqual(
                    paragraph.xpath('./a:pPr/a:lnSpc/*/@val', namespaces=NS),
                    template_paragraph.xpath('./a:pPr/a:lnSpc/*/@val', namespaces=NS))
                self.assertEqual(
                    paragraph.xpath('./a:r/a:rPr/@sz', namespaces=NS)[:1],
                    template_paragraph.xpath('./a:r/a:rPr/@sz', namespaces=NS)[:1])

    def test_summary_keeps_source_in_report(self):
        used = {page.get('expert') for page in self.report['pages'] if page.get('expert')}
        for expert in self.models['input1']['experts']:
            if expert['name'] not in used:
                continue
            summary = self.report['bio_summaries'][expert['name']][0]
            self.assertGreaterEqual(len(summary['source']), len(summary['selected_social']))
            self.assertEqual(summary['source'], expert['bio'])
            self.assertLessEqual(len(summary['lines']), 12)

    def test_discussion_experts_use_guest_role_label(self):
        # 讨论环节的人物页需要覆盖模板旧标签，统一显示讨论嘉宾。
        guest_pages = [(slide, spec) for slide, spec in zip(self.deck['slides'], self.report['pages'])
                       if spec.get('role') == 'guest']
        self.assertTrue(guest_pages)
        for slide, _ in guest_pages:
            self.assertIn('讨论嘉宾', slide['text'])
            self.assertNotIn('大会讲者', slide['text'])

    def test_mixed_clinical_and_school_credentials(self):
        expert = {'name': '测试', 'hospital': '甲医院', 'bio': [
            '甲医院肿瘤科主任，主任医师博士生导师', '医学博士',
            '某市医学会委员', '中国医学会副主任委员', '某省医学会常委', '某市协会委员']}
        summary = summarize_bio(expert)
        self.assertEqual(summary['lines'][0], '甲医院肿瘤科主任，主任医师')
        self.assertIn('博士生导师', summary['lines'][1])
        self.assertNotIn('医师', summary['lines'][1])
        self.assertEqual(summary['lines'][2], '某市医学会委员')
        self.assertEqual(summary['lines'][3], '中国医学会副主任委员')
        self.assertLessEqual(len(summary['lines']), 12)

    def test_other_field_titles_keep_source_order(self):
        # 其他领域职称依照原资料顺序输出，不按机构范围和职务级别重新排列。
        summary = summarize_bio({'name': '测试', 'hospital': '甲医院', 'bio': [
            '甲医院主任医师', '某市医学会委员', '中华医学会副主任委员', '某省医学会常委']})
        self.assertEqual(summary['social'], ['某市医学会委员', '中华医学会副主任委员', '某省医学会常委'])
        self.assertEqual(summary['lines'][1:4], summary['social'])

    def test_missing_school_is_blank_and_not_invented(self):
        summary = summarize_bio({'name': '测试', 'hospital': '甲医院', 'bio': ['甲医院消化科主治医师', '中国医学会委员']})
        self.assertEqual(summary['lines'][1], '中国医学会委员')
        self.assertIn('学历或学校相关履历', summary['missing'])

    def test_every_summary_has_at_most_twelve_nonempty_lines(self):
        for model in self.models.values():
            for expert in model['experts']:
                summary = summarize_bio(expert)
                self.assertLessEqual(len(summary['lines']), 12, expert['name'])
                self.assertTrue(all(line.strip() for line in summary['lines']), expert['name'])
                self.assertFalse(any(line in {'课题', '专业特长', '学术任职'} for line in summary['lines']), expert['name'])

    def test_degree_before_department_and_tcm_title(self):
        summary = summarize_bio({'name': '测试', 'hospital': '甲医院', 'bio': ['甲医院', '博士放疗科主任中医师']})
        self.assertEqual(summary['clinical'], '甲医院放疗科，主任中医师')
        self.assertEqual(summary['academic'], '博士')

    def test_society_hospital_and_research_text_not_clinical(self):
        summary = summarize_bio({'name': '测试', 'hospital': '某省研究型医院', 'display_hospital': '甲医院',
                                 'bio': ['主任医师', '某省研究型医院协会胃肠分会委员', '从事消化科研究工作', '参与消化科著作编写']})
        self.assertEqual(summary['clinical'], '甲医院，主任医师')
        self.assertIn('科室', summary['missing'])

    def test_slide_objects_keep_geometry(self):
        templates = {s['number']: s for s in self.models['input1']['template']['slides']}
        for slide, spec in zip(self.deck['slides'], self.report['pages']):
            original = {s['id']: s for s in templates[spec['template_slide']]['shapes']}
            for shape in slide['shapes']:
                expected = original[shape['id']]['bbox']
                if spec.get('expert') and shape['id'] == templates[spec['template_slide']]['fields']['photo']:
                    # 照片对象按可见圆角框校正，其余模板对象保持原坐标。
                    frame = photo_frame_shape(templates[spec['template_slide']], original[shape['id']])
                    if frame:
                        expected = frame['bbox']
                self.assertEqual(shape['bbox'], expected)

    def test_replacement_photo_fills_frame(self):
        package = read_package(self.output)
        experts = {expert['name']: expert for expert in self.models['input1']['experts']}
        for slide, spec in zip(self.deck['slides'], self.report['pages']):
            if not spec.get('expert'):
                continue
            source = self.models['input1']['template']['slides'][spec['template_slide'] - 1]
            photo_field = spec['fields'].get('photo')
            person = experts.get(spec['expert'])
            if not photo_field or not person or not person.get('photo'):
                self.assertFalse(any(s['id'] == photo_field for s in slide['shapes']))
                continue
            picture = next(s for s in slide['shapes'] if s['id'] == photo_field)
            source_picture = next(s for s in source['shapes'] if s['id'] == photo_field)
            frame = photo_frame_shape(source, source_picture)
            expected_bbox = frame['bbox'] if frame else source_picture['bbox']
            self.assertEqual(picture['bbox'], expected_bbox)
            _, _, width, height = expected_bbox
            with Image.open(io.BytesIO(package[picture['image']])) as image:
                # 替换后的照片尺寸直接匹配图片框比例，输出不再包含透明补边。
                self.assertAlmostEqual(image.width / image.height, width / height, places=2)
                self.assertEqual(image.mode, 'RGB')
            if source['fields'].get('role'):
                # 角色标签必须位于照片之后，确保PPT渲染时标签显示在照片上层。
                shape_ids = [shape['id'] for shape in slide['shapes']]
                self.assertLess(shape_ids.index(source['fields']['photo']),
                                shape_ids.index(source['fields']['role']))

    def test_template_names_and_ids_are_not_required(self):
        model = self.models['input1']
        package = read_package(model['template']['path'])
        for slide in model['template']['slides']:
            root = xml(package[slide['part']])
            for prop in root.findall('.//p:cNvPr', NS):
                prop.set('id', str(int(prop.get('id')) + 800))
                prop.set('name', '任意对象名称')
            package[slide['part']] = encoded(root)
        target = self.work / 'unseen.pptx'
        with zipfile.ZipFile(target, 'w') as archive:
            for name, data in package.items():
                archive.writestr(name, data)
        detected = analyze_template(target)
        self.assertEqual([s['role'] for s in detected['slides']], [s['role'] for s in model['template']['slides']])
        self.assertGreater(int(detected['slides'][2]['fields']['identity']), 800)

    def test_draft_removes_old_photo(self):
        model = copy.deepcopy(self.models['input'])
        model['options']['draft'] = True
        path = self.work / 'draft.pptx'
        report = generate(model, path)
        deck = read_deck(path)
        pairs = [(s, p) for s, p in zip(deck['slides'], report['pages']) if p.get('expert') == '朱帅']
        self.assertEqual(len(pairs), 1)
        self.assertIn('简介待补充', pairs[0][0]['text'])
        self.assertFalse(any(s['id'] == pairs[0][1]['fields'].get('photo') for s in pairs[0][0]['shapes']))

    def test_time_overlap_becomes_warning(self):
        model = copy.deepcopy(self.models['input1'])
        model['agenda']['events'][1]['time'] = '19:05-19:40'
        _, warnings = validate_model(model)
        self.assertTrue(any(item['code'] == 'event_time_order' for item in warnings))

    def test_ambiguous_photo_becomes_warning(self):
        model = copy.deepcopy(self.models['input1'])
        # 正式模式下保留候选照片提示，同时允许生成当前首选照片。
        model['options']['draft'] = False
        model['experts'][0]['photo_confirmed'] = False
        _, warnings = validate_model(model)
        self.assertTrue(any(item['code'] == 'photo_unconfirmed' for item in warnings))

    def test_duplicate_field_binding_becomes_warning(self):
        model = copy.deepcopy(self.models['input1'])
        fields = model['template']['slides'][2]['fields']
        fields['bio'] = fields['identity']
        _, warnings = validate_model(model)
        self.assertTrue(any(item['code'] == 'template_field_duplicate' for item in warnings))

    def test_template_profile_cache_reused(self):
        model = self.models['input1']
        cache = self.work / 'cache'
        cache.mkdir(exist_ok=True)
        mappings = [{'role': s['role'], 'fields': s['fields']} for s in model['template']['slides']]
        (cache / (model['template']['fingerprint'] + '.json')).write_text(json.dumps(mappings), encoding='utf-8')
        template, agenda, experts = discover(ROOT / 'input1')
        repeated = analyze(template, agenda, experts, self.work / 'cached', cache)
        self.assertTrue(repeated['template']['cache_used'])

    def test_incomplete_template_mapping_is_repaired(self):
        model = copy.deepcopy(self.models['input1'])
        model['template']['slides'][5]['fields'].pop('bio')
        plan = page_plan(model)
        speaker = next(item for item in plan if item['role'] == 'speaker')
        self.assertTrue(speaker['fields'].get('bio'))

    def test_missing_meeting_title_still_generates(self):
        model = copy.deepcopy(self.models['input1'])
        model['agenda']['title'] = ''
        path = self.work / 'missing-title.pptx'
        report = generate(model, path)
        self.assertTrue(path.exists())
        self.assertTrue(any(item['code'] == 'meeting_title_empty' for item in report['issues']))

    def test_recent_templates_all_have_generation_plan(self):
        folder = ROOT / 'input' / 'ppt模板'
        if not folder.exists():
            self.skipTest('最近模板未放入项目')
        for path in folder.glob('*.pptx'):
            with self.subTest(template=path.name):
                model = copy.deepcopy(self.models['input1'])
                model['template'] = analyze_template(path)
                plan = page_plan(model)
                self.assertGreaterEqual(len(plan), 2)
                self.assertEqual(plan[0]['role'], 'cover')
                self.assertEqual(plan[-1]['role'], 'ending')

    def test_ending_page_clears_old_title_and_date(self):
        # 新会议缺少名称和日期时，结束页中的模板旧会议信息也应清空。
        template = ROOT / 'input' / 'ppt模板' / '康缘王立鹏基层多学科串场.pptx'
        if not template.exists():
            self.skipTest('带旧标题和日期的结束页模板未放入项目')
        model = copy.deepcopy(self.models['input1'])
        model['template'] = analyze_template(template)
        model['agenda']['title'] = ''
        model['agenda']['date'] = ''
        target = self.work / 'blank-ending-metadata.pptx'
        generate(model, target)
        ending = read_deck(target)['slides'][-1]['text']
        self.assertNotIn('基层多学科常见病规范化诊疗能力提升系列学术交流会', ending)
        self.assertNotIn('2026年8月27日', ending)

    def test_client_cannot_replace_server_paths(self):
        original = self.models['input1']
        incoming = copy.deepcopy(original)
        incoming['experts'][0]['path'] = 'C:/private/file.docx'
        incoming['template']['path'] = 'C:/private/file.pptx'
        updated = apply_edits(original, incoming)
        self.assertEqual(updated['experts'][0]['path'], original['experts'][0]['path'])
        self.assertEqual(updated['template']['path'], original['template']['path'])

    def test_sources_unchanged(self):
        for path, checksum in self.hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), checksum)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_home_and_config(self):
        self.assertIn('AutoPPT', urllib.request.urlopen(self.url).read().decode())
        data = json.load(urllib.request.urlopen(self.url + '/api/config'))
        self.assertEqual(data['token'], TOKEN)

    def test_static_types_ignore_system_file_associations(self):
        # 模拟系统将扩展名关联为普通文本，脚本与样式仍须返回浏览器认可的类型。
        with patch('mimetypes.guess_type', return_value=('text/plain', None)):
            for path, mime in (('/', 'text/html'), ('/app.js', 'text/javascript'), ('/style.css', 'text/css')):
                with self.subTest(path=path), urllib.request.urlopen(self.url + path) as response:
                    self.assertEqual(response.headers['Content-Type'], mime + '; charset=utf-8')
                    self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                    self.assertTrue(response.read())

    def test_cross_site_post_blocked(self):
        request = urllib.request.Request(self.url + '/api/sample', data=b'{"name":"input1"}', method='POST')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 403)

    def test_directory_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url + '/../../app.py')
        self.assertEqual(caught.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
