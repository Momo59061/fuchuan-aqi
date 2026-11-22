# 富川瑶族自治县气象文本生成器

单页 Streamlit 应用：选择模板、日期、控制系统、实况/预报时段，自动拉取整点数据并按业务模板生成
"天气实况及污染物气象扩散条件预报"文案；结果可在线编辑、一键复制或下载。

## 模板

| 模板 | 实况 | 预报 | 页脚 |
|---|---|---|---|
| 08 时模板 | 昨天 20 时 → 今天 08 时（或今天 08-12 时） | 今天白天 08-20 时（或今天夜间 20 时 → 次日 08 时） | 基准日 08 时 |
| 22 时模板 | 今天 20-22 时（或昨天 20-22 时） | 今天晚上 22 时 → 次日 08 时 | 基准日 22 时 |
| 整点模板 | 默认发布前 2 小时 | 默认发布后 2 小时 | 发布日 发布时 |

整点模板的时段可在"自定义时段"中修改，适配"每两小时一次、加密或延后"的发布节奏。

## 数据来源

- **实况**：weather.com 公开接口 `/v3/wx/conditions/historical/hourly/1day`，返回过去 24 小时逐小时实况，
  包含今天已经过去的整点。
- **预报**：weather.com 公开接口 `/v3/wx/forecast/hourly/2day`，返回未来 48 小时逐小时预报。
- **扩散条件**：无自动数据源，由页面下拉框选择（一般 / 好 / 较好 / 一般到较差 / 较差 / 差），
  生成后改动下拉框会自动按整点表重算文本；如需"一般到较差"这类区间描述直接选择即可。

规则：窗口内已有实况的整点取实况，其余整点取 TWC 预报。所有整点数据在页面的数据表中
均可逐格修改或粘贴覆盖，修改后文本随即按表格重新统计；拿不到的整点显示【待填】，可在文本框中手工补。

## 本地运行

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
# 编辑 .streamlit\secrets.toml 填入真实凭据
.venv/Scripts/streamlit run app.py
```

运行纯函数测试（无需网络）：

```bash
pip install pytest
pytest tests
```

## 部署到 Streamlit Community Cloud

### 1. 本地提交到 Git

```bash
git init
git branch -M main
git add app.py requirements.txt README.md .gitignore .streamlit/secrets.toml.example tests/test_pure.py
git commit -m "富川扩散条件预报文本生成器"
```

`.streamlit/secrets.toml`、`.venv/`、`.claude/` 已在 `.gitignore` 中，不会被提交。

### 2. 推送到 GitHub

1. 登录 GitHub → **New repository**，仓库名如 `fuchuan-aqi`，**不要**勾选初始化
   README / .gitignore（本地已有）。
2. 本地执行：

```bash
git remote add origin https://github.com/<你的用户名>/fuchuan-aqi.git
git push -u origin main
```

私有仓库也可以，Streamlit Cloud 首次连接时会请求授权。

### 3. 在 Streamlit Cloud 创建应用

1. 打开 [share.streamlit.io](https://share.streamlit.io)，用 GitHub 账号登录。
2. **Create app → Deploy a public app from GitHub**，填写：
   - Repository：`<你的用户名>/fuchuan-aqi`
   - Branch：`main`
   - Main file path：`app.py`
   - App URL：自定义子域名，如 `fuchuan-aqi`（最终地址 `https://fuchuan-aqi.streamlit.app`，
     被占用时换一个）。
3. 展开 **Advanced settings → Secrets**，粘贴：

   ```toml
   TWC_KEY = "把 32 位 key 填在这里"
   # 可选
   TWC_GEOCODE = "24.897,111.324"
   ```

   也可以留空——代码内置了默认 key 和坐标。

4. 点 **Deploy**。首次构建需安装依赖（`pandas` 较大），约 3–10 分钟，进度与日志在右上
   **Manage app → Logs** 查看。

### 4. 上线后验证（手机和电脑各做一遍）

1. 打开应用地址，确认表单单列显示、日期选择器可点。
2. 点"生成文本"：08 模板应输出实况 + 预报两段；切换"大气污染物气象扩散条件"下拉框，
   文本应立即跟着变。
3. 展开"整点数据"：确认 24 条实况和预报整点有时间、数值和"来源"；改一个温度，文本自动重算。
4. 手工编辑文本框 → 点"复制全文" → 粘贴到手机备忘录，确认粘贴的是修改后的内容；
   再点"下载 .txt"核对内容一致。

### 5. 日常维护

- 更新代码：本地 `git push` 即可，Cloud 自动重新部署（约 1–3 分钟）。
- 免费层闲置 12 小时后休眠，再次访问需点 "Get this app back up"，冷启动约 1 分钟。
- key 失效（页面提示 `weather.com 接口返回 HTTP 401/403`）：打开
  [富川今日天气页](https://weather.com/zh-CN/cn/guangxi/hezhou/fuchuan-yao-autonomous-county/postcode/542700/today)，
  在浏览器查看网页源码，搜索 `api.weather.com` 或 `"apiKey"`，把新的 32 位 key 填进
  Dashboard 的 Secrets 并保存，应用会自动重启。
- 排错看 Dashboard → 应用 → **Manage app → Logs**。

### 常见问题

- **构建慢**：首次要装 `pandas` 等依赖属正常，不要为了提速删掉 pandas，数据表依赖它。
- **时间不对**：Cloud 服务器是 UTC，代码已统一按北京时间（`Asia/Shanghai`）处理。
- **想限制访问**：免费层没有密码保护，只能公开链接；需要口令请改用其他托管方式。
- **不要把 `.streamlit/secrets.toml` 推到 GitHub**；若不慎推送，立即更换 key 并清理提交历史。

## 目录

```
app.py                         应用（单文件）
requirements.txt               依赖
.streamlit/secrets.toml.example  凭据模板
tests/test_pure.py             纯函数测试
```
