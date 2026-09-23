# 2026-05-09 开发日志

## 完成事项
- [x] 创建项目目录结构
- [x] 安装 Node.js v22.14.0
- [x] 安装 Python 依赖 PySide6 + pyperclip
- [x] 编写四份标准文档（需求、技术、设计、开发指南）
- [x] 编写 CLAUDE.md 项目指引
- [x] Phase 2-5 全部功能实现完毕：
  - [x] 剪贴板监控（QTimer 500ms 轮询）
  - [x] SQLite 存储（文字 + 图片 base64）
  - [x] 卡片式 UI（搜索、置顶、删除）
  - [x] 系统托盘 + Alt+V 全局快捷键
  - [x] 自动粘贴（模拟 Ctrl+V）
  - [x] 设置面板（保留期限 + 隐私排除）
- [x] python main.py 启动成功

## 待办事项
- [ ] Phase 6: 安装 PyInstaller 并打包为 exe
- [ ] 全功能回归测试
- [ ] 用户实际使用测试

## 遇到的问题
1. Electron 42/35/28 的 `require('electron')` 均解析到 npm 包的 path 字符串而非 API
   - **解决**：切换到 Python + PySide6，只需 pip install 即可运行
2. Node.js 未安装
   - **解决**：下载 ZIP 解压到 `%LOCALAPPDATA%\Programs\nodejs`
3. PySide6 安装慢（168MB）
   - **解决**：使用清华 PyPI 镜像（pypi.tuna.tsinghua.edu.cn）
