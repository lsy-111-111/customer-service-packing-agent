# 第三方依赖与资源

根目录 MIT 许可证适用于本项目自有代码，不替代第三方软件和素材的许可。

- **Noto Sans SC 字体**：`backend/app/assets/fonts/NotoSansSC.ttf`，采用 SIL Open Font License 1.1。完整许可保留于同目录 `OFL.txt`，来源和转换说明见该目录 `README.md`。
- **PDF.js**：由 `pdfjs-dist` 依赖提供。环境准备会从安装包复制 `cmaps`、`standard_fonts`、`wasm`、`iccs` 及随附许可文件，生成到 `frontend/public/pdfjs/`。这些生成文件不纳入 Git。
- **Python 和 JavaScript 依赖**：分别见 `backend/requirements.txt`、`frontend/package.json` 和 `frontend/pnpm-lock.yaml`。每个依赖使用其发布包附带的许可；本仓库不重新许可这些依赖。
- **PyMuPDF / MuPDF**：使用其 AGPL 或商业许可条款，不属于本项目 MIT 许可覆盖范围。请查看所安装版本附带的完整许可及上游说明：https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright 。

内置模板、顾客、订单和商品资料用于演示，未包含原使用者的业务记录。
