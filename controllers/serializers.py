# -*- coding: utf-8 -*-
import datetime
from itertools import chain

from .parser import Parser
from .exceptions import QueryFormatError

# 序列化时默认排除的敏感字段（黑名单）。
# 即使客户端使用 {*}/all 查询，这些字段也不会被返回；
# 客户端必须显式按名请求才能获取（仍受 Odoo ACL 约束）。
SENSITIVE_FIELD_NAMES = {
    "password",
    "password_crypt",
    "secret",
    "api_key",
    "access_token",
    "refresh_token",
    "token",
    "private_key",
    "pg_key",
    "totp_secret",
}

# 字段名包含以下片段也视为敏感（如 confirm_password、oauth_access_token）
SENSITIVE_FIELD_FRAGMENTS = ("password", "secret", "token", "private_key")


def is_sensitive_field(field_name):
    """判断字段是否属于敏感字段"""
    name = (field_name or "").lower()
    if name in SENSITIVE_FIELD_NAMES:
        return True
    return any(frag in name for frag in SENSITIVE_FIELD_FRAGMENTS)


class Serializer:
    # 本次序列化中被"显式按名"请求的字段集合（线程内类属性，
    # 在 serialize 入口设置、build_flat_field 中消费）
    _explicitly_requested = frozenset()
    # P1（N+1）：当前模型/记录的 fields_get 缓存（每次 serialize 重新拉取一次，
    # build_flat_field / build_nested_field 直接命中，避免逐字段 RPC）
    _fields_info = None
    _fields_model = None

    def __init__(self, record, query="{*}", many=False):
        self.many = many
        self._record = record
        self._raw_query = query
        super().__init__()

    def get_parsed_restql_query(self):
        parser = Parser(self._raw_query)
        try:
            parsed_restql_query = parser.get_parsed()
            return parsed_restql_query
        except SyntaxError as e:
            msg = "QuerySyntaxError: " + str(e)
            raise SyntaxError(msg) from None
        except QueryFormatError as e:
            msg = "QueryFormatError: " + str(e)
            raise QueryFormatError(msg) from None

    @property
    def data(self):
        parsed_restql_query = self.get_parsed_restql_query()
        if self.many:
            # P1（N+1）：批量场景先一次性 prefetch 记录与常用字段值，
            # 再逐条序列化，避免 ORM 惰性加载产生的逐行查询。
            records = list(self._record)
            if records:
                try:
                    # 触发 Odoo prefetch：同一批记录的可缓存字段一次性加载
                    records[0].browse([r.id for r in records]).prefetch_ids
                except Exception:
                    pass  # prefetch 仅是优化，失败不影响正确性
            return [self.serialize(rec, parsed_restql_query) for rec in records]
        return self.serialize(self._record, parsed_restql_query)

    @classmethod
    def build_flat_field(cls, rec, field_name):
        all_fields = rec.fields_get_keys()
        if field_name not in all_fields:
            msg = "'%s' field is not found" % field_name
            raise LookupError(msg)
        # 敏感字段黑名单兜底：除非显式按名请求，否则默认全字段查询不返回
        if field_name not in cls._explicitly_requested and is_sensitive_field(field_name):
            return {}
        # P1（N+1）：字段类型统一从预取的 fields_info 读取，避免逐字段调用
        # rec.fields_get(field_name)；fields_info 由 serialize() 每次序列化
        # 仅拉取一次并沿记录/嵌套传递。
        meta = (cls._fields_info or {}).get(field_name)
        if meta is None:
            meta = rec.fields_get(field_name).get(field_name) or {}
        field_type = meta.get("type")
        if field_type in ["one2many", "many2many"]:
            return {field_name: [record.id for record in rec[field_name]]}
        elif field_type in ["many2one"]:
            return {field_name: rec[field_name].id if rec[field_name] else None}
        elif field_type == "datetime" and rec[field_name]:
            return {field_name: rec[field_name].strftime("%Y-%m-%d %H:%M:%S")}
        elif field_type == "date" and rec[field_name]:
            return {field_name: rec[field_name].strftime("%Y-%m-%d")}
        elif field_type == "time" and rec[field_name]:
            return {field_name: rec[field_name].strftime("%H:%M:%S")}
        elif (
            field_type == "binary"
            and isinstance(rec[field_name], bytes)
            and rec[field_name]
        ):
            return {field_name: rec[field_name].decode("utf-8")}
        else:
            return {field_name: rec[field_name]}

    @classmethod
    def build_nested_field(cls, rec, field_name, nested_parsed_query):
        all_fields = rec.fields_get_keys()
        if field_name not in all_fields:
            msg = "'%s' field is not found" % field_name
            raise LookupError(msg)
        # P1（N+1）：优先使用 serialize() 预取的字段元数据
        meta = (cls._fields_info or {}).get(field_name)
        if meta is None:
            meta = rec.fields_get(field_name).get(field_name) or {}
        field_type = meta.get("type")
        if field_type in ["one2many", "many2many"]:
            return {
                field_name: [
                    cls.serialize(record, nested_parsed_query)
                    for record in rec[field_name]
                ]
            }
        elif field_type in ["many2one"]:
            if rec[field_name]:
                return {field_name: cls.serialize(rec[field_name], nested_parsed_query)}
            else:
                return {field_name: None}
        else:
            # Not a nested field
            msg = "'%s' is not a nested field" % field_name
            raise ValueError(msg)

    @classmethod
    def serialize(cls, rec, parsed_query):
        # 记录本次查询中"显式按名"请求的字段：
        # 敏感字段仅在被显式点名时才返回，通配符展开不视为显式请求。
        explicit = {f for f in (parsed_query.get("include") or []) if isinstance(f, str)}
        explicit |= set(parsed_query.get("exclude") or [])
        prev = cls._explicitly_requested
        prev_info, prev_model = cls._fields_info, cls._fields_model
        cls._explicitly_requested = explicit
        try:
            # P1（N+1）：每个模型只拉取一次完整 fields_get 并缓存，供所有记录/
            # 字段复用；嵌套序列化切换到关联模型时缓存随之切换，退出时恢复。
            if cls._fields_model != rec._name:
                cls._fields_info = rec.fields_get()
                cls._fields_model = rec._name
            return cls._serialize(rec, parsed_query)
        finally:
            cls._explicitly_requested = prev
            cls._fields_info, cls._fields_model = prev_info, prev_model

    @classmethod
    def _serialize(cls, rec, parsed_query):
        data = {}

        # NOTE: self.parsed_restql_query["include"] not being empty
        # is not a guarantee that the exclude operator(-) has not been
        # used because the same self.parsed_restql_query["include"]
        # is used to store nested fields when the exclude operator(-) is used
        if parsed_query["exclude"]:
            # Exclude fields from a query
            all_fields = rec.fields_get_keys()
            for field in parsed_query["include"]:
                if field == "*":
                    continue
                if isinstance(field, dict):
                    for nested_field, nested_parsed_query in field.items():
                        built_nested_field = cls.build_nested_field(
                            rec, nested_field, nested_parsed_query
                        )
                        data.update(built_nested_field)

            flat_fields = set(all_fields).symmetric_difference(
                set(parsed_query["exclude"])
            )
            for field in flat_fields:
                flat_field = cls.build_flat_field(rec, field)
                data.update(flat_field)

        elif parsed_query["include"]:
            # Here we are sure that self.parsed_restql_query["exclude"]
            # is empty which means the exclude operator(-) is not used,
            # so self.parsed_restql_query["include"] contains only fields
            # to include
            all_fields = rec.fields_get_keys()
            if "*" in parsed_query["include"]:
                # Include all fields
                parsed_query["include"] = list(
                    filter(lambda item: item != "*", parsed_query["include"])
                )
                fields = chain(parsed_query["include"], all_fields)
                parsed_query["include"] = list(fields)

            for field in parsed_query["include"]:
                if isinstance(field, dict):
                    for nested_field, nested_parsed_query in field.items():
                        built_nested_field = cls.build_nested_field(
                            rec, nested_field, nested_parsed_query
                        )
                        data.update(built_nested_field)
                else:
                    flat_field = cls.build_flat_field(rec, field)
                    data.update(flat_field)
        else:
            # The query is empty i.e query={}
            # return nothing
            return {}
        return data
