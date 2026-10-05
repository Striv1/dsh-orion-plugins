# 测试资料

`synthetic_chinese_text.pdf` 是专门为本仓库编写的两页合成文件。正文、标题和元数据均由下面的代码生成，不来自客户、业务资料或真实验收记录；它不是本体工程或业务流程通过验收的证据。

文件用于 `test_unstructured_router.py` 中的 macOS Swift/PDFKit 真实文字层提取测试：两页均可直接提取、合计至少 700 个非空白字符，并保留中文及分页顺序。其他平台按现有测试条件跳过此项。

以下命令在仓库根目录运行。重新生成需要 ReportLab 4.4.9；运行现有测试不需要 ReportLab。使用 PDF 标准中文 CID 字体 `STSong-Light`，不附带或嵌入任何系统字体文件。`invariant=True` 固定生成元数据，避免写入本机时间、用户名或路径。

```python
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

lines = [
    "本文件由测试代码生成，所有段落都是专门编写的合成内容。",
    "文字只用于验证中文字符读取，不对应任何实际业务制度。",
    "这里没有客户名称、人员信息、联系方式或其他私人资料。",
    "页面包含可选择的文字层，读取时应直接提取而无需识图。",
    "第一类检查确认文字可以读取，第二类检查确认页面数量。",
    "标点符号包括逗号、句号与括号（测试），均应保持可读。",
    "数字样例为一二三四五，英文字母样例使用 TEST 与 DATA。",
    "不同段落依次排列，提取结果应当遵循原有的阅读顺序。",
    "每页保留独立的标题与页码，便于判断是否存在遗漏内容。",
    "所有示例语句只描述文件测试，不触发审批或外部操作。",
    "这里没有来源授权、发布许可或真实业务结论可供复用。",
    "无需模型账户、网络连接或外部服务即可读取这些文字。",
    "测试成功只说明此文件的文字层可以被当前提取器读取。",
    "它不能证明扫描识别、复杂表格解析或业务问答已经完成。",
    "本段保留足够的连续中文文本，用于验证字符数量统计。",
    "这是本页的最后一行合成内容，后续应按页码继续读取。",
]
titles = ["中文文本提取测试", "跨页内容保持顺序"]

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
target = Path("tests/fixtures/synthetic_chinese_text.pdf")
pdf = canvas.Canvas(str(target), pagesize=A4, invariant=True)
pdf.setTitle("Synthetic Chinese text extraction fixture")
pdf.setAuthor("ORION test suite")
pdf.setSubject("Synthetic fixture; no business or personal data")
pdf.setCreator("tests/fixtures/README.md")
pdf.setKeywords("synthetic, test, Chinese, text extraction")
for page_number, title in enumerate(titles, start=1):
    pdf.setFont("STSong-Light", 19)
    pdf.drawString(54, 776, title)
    pdf.setFont("STSong-Light", 12)
    pdf.drawString(54, 744, "合成测试文件 - 仅用于中文文字层提取")
    for index, line in enumerate(lines):
        pdf.drawString(54, 700 - index * 27, line)
    pdf.setFont("STSong-Light", 11)
    pdf.drawString(54, 56, f"第 {page_number} 页，共 2 页")
    pdf.showPage()
pdf.save()
```

`ontop-runtime/` 的合成 SQL 映射测试资料另见该目录的 README。
