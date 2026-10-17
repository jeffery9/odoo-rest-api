# -*- coding: utf-8 -*-
"""REST API 幂等键存储模型（I1）。

POST /{model}、POST /{model}/batch 及方法调用端点支持可选的
`Idempotency-Key` 请求头。首次请求成功后，将响应体快照存入本模型；
携带相同 key + 相同作用域（用户/方法/路径/请求体哈希）的重放请求
直接返回缓存副本（附 X-Idempotent-Replay: true 响应头），不再执行
创建逻辑——网络重试因此不会产生重复记录。

key 相同但请求体不同视为客户端错误（409），防止跨请求误用同一 key。
记录带过期时间，由调度任务定期清理（默认保留 7 天）。

访问权限：仅创建者本人可读自己的条目，root/系统管理员全量可见；
表上 (user_id, scope) 唯一约束保证并发下的原子占位。
"""
import datetime
import hashlib
import json

from odoo import api, fields, models, _


class IrRestApiIdempotency(models.Model):
    _name = "ir.rest.api.idempotency"
    _description = "REST API Idempotency Key Store"
    _order = "create_date desc"

    # 同用户同作用域内 key 唯一；不同用户的同名 key 互不影响
    _sql_constraints = [
        ("scope_uniq", "unique(user_id, scope)",
         "An idempotency key can only be used once per user."),
    ]

    user_id = fields.Many2one(
        "res.users", string="User", required=True, index=True, ondelete="cascade")
    scope = fields.Char(string="Scope Hash", required=True, index=True)
    model_name = fields.Char(string="Target Model", readonly=True)
    request_path = fields.Char(string="Request Path", readonly=True)
    http_method = fields.Char(string="HTTP Method", readonly=True, default="POST")
    payload_hash = fields.Char(string="Payload SHA-256", readonly=True)
    response_json = fields.Text(string="Cached Response", readonly=True)
    http_status = fields.Integer(string="Response Status", readonly=True, default=200)
    expires_at = fields.Datetime(string="Expires At", index=True)

    @api.model
    def build_scope(self, path, http_method, payload):
        """计算作用域哈希：path + method + 规范化 JSON payload 的 sha256。"""
        try:
            canonical = json.dumps(payload, sort_keys=True,
                                   separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            canonical = repr(payload)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{http_method.upper()} {path} {digest}", digest

    @api.model
    def find_or_reserve(self, path, http_method, payload):
        """尝试为请求占位。

        返回 (record, status)，status ∈ {"fresh", "replay", "conflict", "pending"}：
          - fresh:    已插入占位行，调用方应执行业务并 save_response()
          - replay:   已有完成条目且 payload 一致，返回缓存副本
          - conflict: 已有条目但 payload 不一致（key 误用）→ 409
          - pending:  首次请求仍在处理中（占位未回填）→ 409，客户端稍后重试
        """
        scope, digest = self.build_scope(path, http_method, payload)
        existing = self.sudo().search(
            [("user_id", "=", self.env.uid), ("scope", "=", scope)], limit=1)
        if existing:
            if existing.payload_hash != digest:
                return existing, "conflict"
            if not existing.response_json:
                return existing, "pending"
            return existing, "replay"
        vals = {
            "user_id": self.env.uid,
            "scope": scope,
            "model_name": path.split("/")[2] if len(path.split("/")) > 2 else False,
            "request_path": path,
            "http_method": http_method.upper(),
            "payload_hash": digest,
            "expires_at": fields.Datetime.now()
            + datetime.timedelta(days=7),
        }
        try:
            rec = self.sudo().create(vals)
        except Exception:
            # 并发下唯一约束冲突：回滚当前事务后重新查询走 replay/conflict 分支。
            # 注意：此时本请求尚未产生任何业务写入（占位发生在执行业务之前），
            # 全量 rollback 是安全的；重试请求将命中已有条目而非重复占位。
            self.env.cr.rollback()
            existing = self.sudo().search(
                [("user_id", "=", self.env.uid), ("scope", "=", scope)], limit=1)
            if existing:
                if existing.payload_hash != digest:
                    return existing, "conflict"
                return existing, ("replay" if existing.response_json else "pending")
            raise
        return rec, "fresh"

    def save_response(self, payload_dict, http_status=200):
        """业务执行成功后回填响应快照（占位行转为完成态）。"""
        self.sudo().write({
            "response_json": json.dumps(payload_dict, default=str),
            "http_status": http_status,
        })

    @api.autovacuum
    def _gc_expired(self):
        cutoff = fields.Datetime.now()
        expired = self.sudo().search([("expires_at", "!=", False),
                                      ("expires_at", "<", cutoff)])
        expired.unlink()
        return True
