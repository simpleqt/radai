# RADAI 智能体平台 — 仓库说明

本仓库是原 RADAI 单体拆分后的**智能体平台**。知识库/自研入库/自研检索
已拆分至独立项目 [omnigraph](https://github.com/simpleqt/omnigraph);
本仓库不包含任何 RAG/知识库实现,全部知识库访问通过 HTTP(见
`docs/split_agent_rag_services.md` 与 `backend/application/knowledge/internal_client.py`)。

## 核心约束

- 部署前提:omnigraph 知识服务已启动;两侧 `INTERNAL_API_SECRET` 一致;
  `KNOWLEDGE_SERVICE_BASE_URL` 指向该服务(默认 `http://127.0.0.1:8086`)。
- 共享外部设施(MySQL/Redis/MinIO)但职责分离:本仓库只写智能体域数据
  (会话/技能/题库/用户文件);知识库域数据由 omnigraph 写入。
- 严禁在 Git 中提交密码、令牌、连接串、私钥、`.env`、日志、上传文件、
  模型数据或运行期状态。
- 拆分完整性门禁:`make check-imports` 必须通过(校验不引用已移出的知识库模块)。

## 常用命令

```bash
make install        # 安装依赖
make run-api        # 智能体 API (8082)
make run-guest-api  # 访客 API (8083)
make check-imports  # 导入闭包校验
make test           # 测试
```
