# 后端平台工程师线上答题包

本仓库只发布线上招聘题目和第一题运行材料，不接收个人答案。请先阅读两份题面，再创建您自己的 GitHub 私有作答仓库提交答案。不要在本公开仓库创建分支、Pull Request 或提交个人答案。

## 题目

- [第一题 Markdown](题目/第一题.md) · [第一题 Word](题目/第一题.docx)
- [第二题 Markdown](题目/第二题.md) · [第二题 Word](题目/第二题.docx)

两份题面以附件版本为准。第一题是可运行的后端实现题；第二题提交简要整体方案和一个自行选择的一期核心业务闭环，不要求编码。

## 第一题运行材料

第一题材料目录提供题面引用的虚拟数据、旧原型和公开检查器：

- `case.json`：题目输入
- `legacy_trace.csv`、`legacy_result.json`：旧原型记录和结果
- `legacy_backend.py`：旧原型
- `check_result.py`：结果合法性检查器
- `platform_contract.md`：平台接入公开契约
- `platform_requests.json`：请求示例
- `check_platform.py`：平台边界检查器

工具只需 Python 3.10 及以上版本，无第三方依赖。示例：

```text
python3 第一题材料/check_result.py 第一题材料/case.json 第一题材料/legacy_result.json
python3 第一题材料/legacy_backend.py --case 第一题材料/case.json --db baseline.db --out baseline.json
```

候选人实现应按第一题题面提供自己的入口、导出结果和 `adapter.json`，并运行公开检查器。

## 提交提醒

第一题和第二题都请提交到您自己的 GitHub 私有作答仓库。请将仓库链接发给招聘方，并邀请 GitHub 账号 `dldldl888`，以便面试官查看；不要向本公开题库发起 Pull Request。请保留主要工作过程的提交记录。

最终仓库应包含题面要求的源码、运行证据、说明、`AI_USAGE.md` 、必要的 AI 核查依据及个人简历。不要提交密钥、账号、公司内部文件或完整私人聊天记录。
