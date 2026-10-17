# -*- coding: utf-8 -*-
"""OpenAPI 3.0.3 规格 —— REST API 的单一事实来源。

由 /api/v1/openapi.json 端点及启动期自检使用。此前内嵌在
controllers/main.py 中的 _generate_paths() 只声明了 7 个操作，与实际
实现的 18 个端点严重脱节（缺 batch/search/fields/health/info、无 PATCH、
SecurityScheme 类型错误、响应 Schema 与统一信封格式不符），本模块将其
整体替换为与实现逐字段对齐的完整文档。
"""
import copy

API_VERSION = "1.0.0"

# 与 OdooAPI.ALLOWED_METHODS 保持一致（修改时需同步）。
SPEC_ALLOWED_METHODS = [
    "action_confirm", "action_done", "action_cancel", "action_draft",
    "button_confirm", "button_done", "button_cancel", "validate",
    "invalidate_cache", "get_records", "name_search", "read_group",
]


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _param(name, location="query", schema=None, required=False, description=None):
    p = {"name": name, "in": location, "required": required,
         "schema": schema or {"type": "string"}}
    if description:
        p["description"] = description
    return p


MODEL_P = _param("model", "path", required=True,
                 description="Odoo 模型名（如 res.partner）")
ID_P = _param("id", "path", {"type": "integer"}, required=True,
              description="记录 ID")
FUNCTION_P = _param("function", "path", required=True,
                    description="方法名（须在白名单内）")
FIELD_P = _param("field", "path", required=True,
                 description="二进制字段名")
QUERY_P = _param("query", "query", {"type": "string", "default": "{*}"},
                 description="字段选择查询，如 {id,name}；兼容别名 fields。"
                             "通配符展开自动排除敏感字段（password/token/secret 等）")
LIMIT_P = _param("limit", "query", {"type": "integer", "default": 80,
                                    "minimum": 1, "maximum": 1000},
                 description="未分页时返回的最大条数（上限 1000）")
PAGE_SIZE_P = _param("page_size", "query", {"type": "integer", "minimum": 1,
                                            "maximum": 1000},
                     description="每页条数；显式传入时分页语义生效并覆盖 limit")
PAGE_P = _param("page", "query", {"type": "integer", "default": 1, "minimum": 1},
                description="页码（从 1 开始）")
ORDER_P = _param("order", "query",
                 description="排序：JSON 数组字符串或 'name asc, id desc' 形式")
FILTER_P = _param("filter", "query",
                  description="JSON 编码的 domain，如 [[\"name\",\"ilike\",\"x\"]]；"
                              "禁止引用敏感字段")
IDS_Q_P = _param("id", "query",
                 description="记录 ID：单个整数、逗号分隔或 JSON 数组")
IDEMPOTENCY_P = {
    "name": "Idempotency-Key", "in": "header", "required": False,
    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
    "description": "幂等键（I1）：相同 key + 用户 + 请求体的重复 POST 直接返回首次缓存"
                   "响应副本并附 X-Idempotent-Replay: true 头；key 相同但请求体不同返回 409。"
                   "建议 UUIDv4。",
}

ERROR_RESPONSES = {
    "400": {"description": "请求参数/查询语法错误", "content": {
        "application/json": {"schema": _ref("ErrorResponse")}}},
    "401": {"description": "缺少或无效的认证凭证", "content": {
        "application/json": {"schema": _ref("ErrorResponse")}}},
    "403": {"description": "权限不足 / 访问敏感字段 / 方法不在白名单", "content": {
        "application/json": {"schema": _ref("ErrorResponse")}}},
    "404": {"description": "资源不存在或对当前用户不可见（不区分二者）", "content": {
        "application/json": {"schema": _ref("ErrorResponse")}}},
    "500": {"description": "服务器内部错误（不回显内部异常细节）", "content": {
        "application/json": {"schema": _ref("ErrorResponse")}}},
}


def _op(summary, description, responses, parameters=None, request_body=None,
        security=None, operation_id=None):
    op = {"summary": summary, "description": description,
          "parameters": parameters or [],
          "responses": responses}
    if request_body:
        op["requestBody"] = request_body
    if security is not None:
        op["security"] = security
    if operation_id:
        op["operationId"] = operation_id
    return op


def _json_body(schema_name, required=True, description=None):
    body = {"required": required, "content": {
        "application/json": {"schema": _ref(schema_name)}}}
    if description:
        body["description"] = description
    return body


def _success(data_desc, extra_props=None, status="200"):
    props = {
        "success": {"type": "boolean", "example": True},
        "message": {"type": "string"},
        "timestamp": {"type": "string", "format": "date-time"},
        "data": {"type": "object", "description": data_desc},
    }
    if extra_props:
        props["data"]["properties"] = extra_props
    return {status: {"description": "Success", "content": {
        "application/json": {"schema": {
            "type": "object", "properties": props}}}}}


def _build_paths():
    ok_200 = {"200": {"description": "Success", "content": {
        "application/json": {"schema": _ref("SuccessEnvelope")}}}}

    paths = {}

    # ---- /{model} 集合级 ------------------------------------------------
    paths["/{model}"] = {
        "get": _op(
            "List records",
            "获取多条记录。支持字段选择、domain 过滤、排序与分页。"
            "list 端点的 filter 为 query 参数（区别于 search 端点的 body）。"
            "敏感字段默认排除且不可显式点名。",
            {**ok_200, **ERROR_RESPONSES},
            [MODEL_P, QUERY_P, FILTER_P, ORDER_P, LIMIT_P, PAGE_SIZE_P, PAGE_P],
            operation_id="listRecords"),
        "post": _op(
            "Create record",
            "创建单条记录。请求体：{\"data\": {...}, \"context\": {...}}。"
            "context 仅接受白名单键（其余剔除并记日志，S4）。"
            "POST 非天然幂等，强烈建议携带 Idempotency-Key（I1）；"
            "重放时返回首次响应的缓存副本。",
            {**_success("新建记录（含 id）"),
             "409": {"description": "Idempotency-Key 已存在但请求体不一致",
                     "content": {"application/json": {"schema": _ref("ErrorResponse")}}},
             **ERROR_RESPONSES},
            [MODEL_P, IDEMPOTENCY_P],
            _json_body("CreateRequest"), operation_id="createRecord"),
        "put": _op(
            "Bulk update records (partial)",
            "按 filter/ids 批量更新。语义与 PATCH 一致（部分字段更新，I3）："
            "仅覆盖 data 中出现的字段，未出现的保持不变。"
            "批量更新天然幂等。filter 禁止引用敏感字段（S2）。",
            {**_success("更新结果", {"updated": {"type": "integer"},
                                     "records": {"type": "array",
                                                 "items": {"type": "object"}}}),
             **ERROR_RESPONSES},
            [MODEL_P], _json_body("BulkUpdateRequest"), operation_id="bulkUpdate"),
        "patch": _op(
            "Bulk update records",
            "同 PUT：部分字段批量更新（PUT/PATCH 均为 partial-update 语义，I3）。",
            {**_success("更新结果", {"updated": {"type": "integer"},
                                     "records": {"type": "array",
                                                 "items": {"type": "object"}}}),
             **ERROR_RESPONSES},
            [MODEL_P], _json_body("BulkUpdateRequest"), operation_id="bulkUpdatePatch"),
        "delete": _op(
            "Delete multiple records",
            "按 id（query 参数，或 JSON body 的 ids/id）批量删除。"
            "幂等（I2）：目标记录不存在时返回 200 且 deleted=0，而非 404。",
            {**_success("删除结果", {"deleted": {"type": "integer"},
                                     "requested": {"type": "integer"}}),
             **ERROR_RESPONSES},
            [MODEL_P, IDS_Q_P],
            {"required": False, "content": {"application/json": {"schema": _ref("IdsBody")}}},
            operation_id="bulkDelete"),
    }

    # ---- /{model}/{id} 单记录 -------------------------------------------
    paths["/{model}/{id}"] = {
        "get": _op(
            "Get single record",
            "按 ID 获取单条记录，支持 query/fields 字段选择；"
            "对当前用户不可见的记录一律 404（不泄露存在性）。",
            {**_success("记录字段字典"), **ERROR_RESPONSES},
            [MODEL_P, ID_P, QUERY_P], operation_id="getRecord"),
        "put": _op(
            "Update single record (partial)",
            "部分字段更新（与 PATCH 语义一致，I3）：仅覆盖 data 中出现的字段。"
            "重复发送同一请求结果相同（天然幂等）。写操作受 write 记录规则约束（S1）。",
            {**_success("更新后的记录"), **ERROR_RESPONSES},
            [MODEL_P, ID_P], _json_body("UpdateRequest"), operation_id="updateRecord"),
        "patch": _op(
            "Update single record",
            "同 PUT：部分字段更新。",
            {**_success("更新后的记录"), **ERROR_RESPONSES},
            [MODEL_P, ID_P], _json_body("UpdateRequest"), operation_id="patchRecord"),
        "delete": _op(
            "Delete single record",
            "删除单条记录。幂等（I2）：记录不存在时返回 200 {deleted: 0}，"
            "客户端重试安全。删除受 unlink 记录规则约束（S1）。",
            {**_success("删除结果", {"deleted": {"type": "integer"}}),
             **ERROR_RESPONSES},
            [MODEL_P, ID_P], operation_id="deleteRecord"),
    }

    # ---- /{model}/{id}/{field} 二进制 ------------------------------------
    paths["/{model}/{id}/{field}"] = {
        "get": _op(
            "Get binary field content",
            "读取记录的指定二进制/文件字段。敏感字段名被拒绝（403）。",
            {"200": {"description": "文件内容", "content": {
                "application/octet-stream": {"schema": {"type": "string",
                                                       "format": "binary"}},
                "text/plain": {"schema": {"type": "string"}}}},
             **ERROR_RESPONSES},
            [MODEL_P, ID_P, FIELD_P], operation_id="getBinaryField"),
    }

    # ---- /{model}/batch 批量创建 -----------------------------------------
    paths["/{model}/batch"] = {
        "post": _op(
            "Create records in batch",
            "批量创建。请求体：{\"data\": [...], \"context\": {...}}。"
            "整个请求作为一个幂等单元（I1）：携带 Idempotency-Key 时，"
            "重放返回首次创建的完整响应缓存，不会二次插入。",
            {**_success("批量创建结果", {"created": {"type": "integer"},
                                         "records": {"type": "array",
                                                     "items": {"type": "object"}}}),
             "409": {"description": "Idempotency-Key 已存在但请求体不一致",
                     "content": {"application/json": {"schema": _ref("ErrorResponse")}}},
             **ERROR_RESPONSES},
            [MODEL_P, IDEMPOTENCY_P],
            _json_body("BatchCreateRequest"), operation_id="batchCreate"),
    }

    # ---- /{model}/search 高级搜索 ----------------------------------------
    paths["/{model}/search"] = {
        "post": _op(
            "Search records",
            "POST 方式的高级搜索：domain 通过请求体传入（区别于 list 端点的 "
            "query filter）。domain 与 fields 均禁止引用敏感字段（S2）。"
            "只读操作，天然幂等。",
            {**_success("搜索结果", {"records": {"type": "array",
                                                 "items": {"type": "object"}},
                                     "count": {"type": "integer"}}),
             **ERROR_RESPONSES},
            [MODEL_P], _json_body("SearchRequest"), operation_id="searchRecords"),
    }

    # ---- /{model}/fields 字段元数据 ---------------------------------------
    paths["/{model}/fields"] = {
        "get": _op(
            "Get model field metadata",
            "返回模型的 fields_get 元数据；敏感字段定义已被剔除（S2）。",
            {**_success("字段元数据", {"model": {"type": "string"},
                                       "fields": {"type": "object",
                                                  "additionalProperties": {
                                                      "type": "object"}}}),
             **ERROR_RESPONSES},
            [MODEL_P], operation_id="getModelFields"),
    }

    # ---- 方法调用（白名单） ------------------------------------------------
    func_body = _json_body("MethodCallRequest")
    call_desc = (
        "调用模型公共方法。采用默认拒绝白名单策略（P0）：私有方法(_开头)→400；"
        "不在白名单→403；方法不存在→404。白名单：%s。副作用取决于具体方法，"
        "调用方应自行评估幂等性并携带 Idempotency-Key。" % ", ".join(SPEC_ALLOWED_METHODS))
    paths["/object/{model}/{function}"] = {
        "post": _op("Call model method", call_desc,
                    {**ok_200, "409": {"description": "Idempotency-Key 冲突",
                                       "content": {"application/json": {
                                           "schema": _ref("ErrorResponse")}}},
                     **ERROR_RESPONSES},
                    [MODEL_P, FUNCTION_P, IDEMPOTENCY_P], func_body,
                    operation_id="callModelMethod"),
    }
    paths["/object/{model}/{id}/{function}"] = {
        "post": _op("Call record method",
                    call_desc + " 记录须对当前用户可见且满足 read 记录规则。",
                    {**ok_200, "409": {"description": "Idempotency-Key 冲突",
                                       "content": {"application/json": {
                                           "schema": _ref("ErrorResponse")}}},
                     **ERROR_RESPONSES},
                    [MODEL_P, ID_P, FUNCTION_P, IDEMPOTENCY_P], func_body,
                    operation_id="callRecordMethod"),
    }

    # ---- 元信息端点 --------------------------------------------------------
    paths["/openapi.json"] = {
        "get": _op(
            "OpenAPI document",
            "本文档。默认公开以便集成方发现；可通过系统参数 "
            "rest_api.openapi_public=false 收紧为仅管理员可访问（S3）。",
            {"200": {"description": "OpenAPI 3.0.3 文档", "content": {
                "application/json": {"schema": {"type": "object"}}}}},
            security=[], operation_id="getOpenAPISpec"),
    }
    paths["/health"] = {
        "get": _op("Health check", "无需认证的存活探针。",
                   {**_success("健康状态", {"status": {"type": "string"},
                                            "version": {"type": "string"},
                                            "odoo_version": {"type": "string"}})},
                   security=[], operation_id="healthCheck"),
    }
    paths["/info"] = {
        "get": _op("API information", "API 版本、认证方式与端点索引（需认证）。",
                   {**_success("API 元信息"), **ERROR_RESPONSES},
                   operation_id="apiInfo"),
    }
    return paths


SCHEMAS = {
    "SuccessEnvelope": {
        "type": "object",
        "description": "统一成功响应信封",
        "properties": {
            "success": {"type": "boolean", "example": True},
            "message": {"type": "string"},
            "data": {"type": "object", "nullable": True},
            "timestamp": {"type": "string", "format": "date-time"},
        },
        "required": ["success", "message", "timestamp"],
    },
    "ErrorResponse": {
        "type": "object",
        "description": "统一错误响应；不向普通用户泄露内部异常细节（details 仅管理员可见）",
        "properties": {
            "success": {"type": "boolean", "example": False},
            "error": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": [
                        "bad_request", "unauthorized", "forbidden", "not_found",
                        "conflict", "internal_error"]},
                    "http_status": {"type": "integer"},
                    "message": {"type": "string"},
                    "type": {"type": "string", "description": "异常类名"},
                    "details": {"type": "string", "description": "仅系统管理员可见"},
                },
                "required": ["code", "http_status", "message"],
            },
            "timestamp": {"type": "string", "format": "date-time"},
        },
        "required": ["success", "error", "timestamp"],
    },
    "ContextObject": {
        "type": "object",
        "description": "可选 Odoo context；仅白名单键生效（lang/uid/timezone/"
                       "allowed_company/bin_size/use_email_template/from_module/"
                       "tz/default_*，S4），其余键被剔除并记日志",
        "additionalProperties": True,
    },
    "DomainFilter": {
        "type": "array",
        "description": "Odoo domain；叶子字段名不得为敏感字段（S2）",
        "items": {},
    },
    "CreateRequest": {
        "type": "object",
        "properties": {
            "data": {"type": "object", "description": "字段名→值映射"},
            "context": _ref("ContextObject"),
        },
        "required": ["data"],
    },
    "BatchCreateRequest": {
        "type": "object",
        "properties": {
            "data": {"type": "array", "items": {"type": "object"},
                     "description": "记录数据数组"},
            "context": _ref("ContextObject"),
        },
        "required": ["data"],
    },
    "UpdateRequest": {
        "type": "object",
        "properties": {
            "data": {"type": "object",
                     "description": "待更新字段；关系字段可用 {\"link\": [ids]} 等操作对象"},
            "context": _ref("ContextObject"),
        },
        "required": ["data"],
    },
    "BulkUpdateRequest": {
        "type": "object",
        "properties": {
            "data": {"type": "object"},
            "filter": _ref("DomainFilter"),
            "ids": {"type": "array", "items": {"type": "integer"},
                    "description": "替代 filter 的目标 ID 列表"},
            "context": _ref("ContextObject"),
        },
        "required": ["data"],
    },
    "IdsBody": {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}},
            "id": {"oneOf": [{"type": "integer"},
                             {"type": "array", "items": {"type": "integer"}}]},
        },
    },
    "SearchRequest": {
        "type": "object",
        "properties": {
            "domain": _ref("DomainFilter"),
            "fields": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "default": 80, "maximum": 1000},
            "offset": {"type": "integer", "default": 0},
            "order": {"type": "string"},
        },
    },
    "MethodCallRequest": {
        "type": "object",
        "properties": {
            "args": {"type": "array", "description": "位置参数"},
            "kwargs": {"type": "object", "description": "关键字参数"},
        },
    },
}


def build_openapi_spec():
    """构建完整的 OpenAPI 3.0.3 文档（纯函数，便于离线校验）。"""
    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "Odoo REST API",
            "version": API_VERSION,
            "description": (
                "Odoo 18 通用 REST API。统一响应信封 {success,message,data,"
                "timestamp}；错误信封 {success:false,error:{code,http_status,"
                "message}}。认证：HTTP API Key（Authorization: Bearer <key>）"
                "或 Basic。所有写端点受模型 ACL + 记录规则双重约束。"),
        },
        "servers": [{"url": "/api/v1"}],
        "tags": [
            {"name": "CRUD", "description": "记录增删改查"},
            {"name": "Query", "description": "搜索与元数据"},
            {"name": "Methods", "description": "白名单方法调用"},
            {"name": "Meta", "description": "健康检查与文档"},
        ],
        "paths": _build_paths(),
        "components": {
            "securitySchemes": {
                "HttpBasic": {"type": "http", "scheme": "basic",
                              "description": "用户名/密码或 API Key 形式的 Basic 认证"},
                "BearerApiKey": {"type": "http", "scheme": "bearer",
                                 "bearerFormat": "ASCII",
                                 "description": "res.users.apikeys (scope=odoo.api)"},
            },
            "schemas": SCHEMAS,
        },
        "security": [{"HttpBasic": []}, {"BearerApiKey": []}],
    }
    return spec


def get_spec_dict():
    return copy.deepcopy(build_openapi_spec())
