# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付和最终核定服务，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据并识别跨案件批量复用
- `POST /api/claims/survey`、`POST /api/claims/submit-review`
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件
- `POST /api/claims/finalize`：锁定最终核定结果

## 赔后回收台账

核定赔付后的残值处置和第三方追偿不再走表外，由 `supervisor` 登记回收记录：

- `POST /api/recoveries`：登记处置方式（`salvage` 残值处置 / `subrogation` 第三方追偿 / `other` 其他回收）、预计回收金额和说明。登记后预计金额即抵减净损失，状态为待回收（`pending`）。
- `POST /api/recoveries/confirm`：凭收款凭证号确认正式到账，可按实际到账金额（缺省取预计金额）入账。案件尚未核定赔付、或累计回收超过核定赔款时拒绝入账并留在待处理；收款凭证号全局唯一，同一凭证不能重复入账。
- `POST /api/claims/close` / `POST /api/claims/reopen`：有待回收款的案件不能结案；已结案件可重开继续追偿，重开后回到已核定状态。
- `GET /api/recoveries`：按案件汇总核定赔款、待回收、已回收和净支出（赔款 − 待回收 − 已回收），并列出每笔回收明细。

`GET /api/state` 同步返回 `recoveries` 记录；回收登记与确认写入 `timeline` 审计。


## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别和角色权限。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
