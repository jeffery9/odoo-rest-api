# -*- coding: utf-8 -*-  
import json  
import math  
import logging  
import datetime  
from datetime import datetime  
  
from odoo import http, exceptions, _, release, Command  
from odoo.http import request, Dispatcher  
from odoo.exceptions import AccessError, AccessDenied, UserError, ValidationError
from odoo.tools import date_utils  
  
from .serializers import Serializer, is_sensitive_field  
from .parser import Parser  
from .exceptions import QueryFormatError  

from .openapi_spec import API_VERSION, build_openapi_spec

from werkzeug.exceptions import (
    HTTPException,
    BadRequest as HTTPBadRequest,
    Forbidden as HTTPForbidden,
    NotFound as HTTPNotFound,
)
  
_logger = logging.getLogger(__name__)


def validate_domain_fields(domain):
    """拒绝在 domain/filter 中引用敏感字段（S2）。

    防止利用搜索/批量更新对 password/token/secret 类列做布尔探测
    （盲注式爆破）。domain 叶子形如 [name, operator, value]，
    也兼容 "|" "&" "!" 等波兰符号项与嵌套结构。
    """
    if not isinstance(domain, (list, tuple)):
        return
    for leaf in domain:
        if isinstance(leaf, (list, tuple)):
            if leaf and isinstance(leaf[0], str) and is_sensitive_field(leaf[0]):
                raise HTTPForbidden(
                    f"Filtering on sensitive field '{leaf[0]}' is not allowed"
                )
            validate_domain_fields(leaf)  
  
  
class RestAPIDispatcher(Dispatcher):  
    """Custom dispatcher for REST API to avoid JSON-RPC interference"""  
    routing_type = 'rest'  
  
    @classmethod  
    def is_compatible_with(cls, request):  
        # Only handle requests to /api/v1/* paths with JSON content type  
        return (request.httprequest.path.startswith('/api/v1/') and   
                request.httprequest.mimetype == 'application/json')  
  
    def dispatch(self, endpoint, args):  
        """Handle REST API requests with JSON body parsing"""  
        # Parse JSON body for all methods carrying a payload
        # (POST/PUT/PATCH，以及允许带 body 的 DELETE)
        if request.httprequest.get_data():  
            try:  
                request.jsonrequest = json.loads(request.httprequest.get_data(as_text=True))  
            except (ValueError, json.JSONDecodeError):  
                request.jsonrequest = {}  
        else:  
            request.jsonrequest = {}  
          
        # Set up params from URL args and query string  
        request.params = dict(request.httprequest.args.to_dict(flat=True), **args)  
          
        # Call the endpoint  
        if request.db:  
            return request.registry['ir.http']._dispatch(endpoint)  
        else:  
            return endpoint(**request.params)  
  
    def handle_error(self, exc):
        """Handle errors for REST API（不向客户端泄露内部异常细节）"""
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        _logger.exception("Unhandled REST API dispatcher error")
        return http.Response(
            json.dumps({
                "success": False,
                "error": {
                    "code": 500,
                    "message": "Internal server error",
                    "type": type(exc).__name__,
                },
            }),
            status=500,
            mimetype="application/json"
        )
  
  
def parse_ids(raw):
    """稳健地解析记录 ID 参数（P1）。

    支持三种输入形态：
      - JSON 数组字符串，如 '[1,2,3]'（推荐写法）
      - 逗号分隔字符串，如 '1,2,3'
      - 原生 list/tuple（来自已解析的 JSON body）

    不再使用 ast.literal_eval：它对非字面量输入抛异常、可接受负数/表达式
    片段，且与 JSON 语义不一致。任何非法值统一抛出 ValueError，由调用方
    转成 400 响应。
    """
    if raw is None:
        raise ValueError("Missing id parameter")
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            raise ValueError("Empty id parameter")
        try:
            values = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            values = text.split(",")
        if not isinstance(values, (list, tuple)):
            values = [values]
        else:
            values = list(values)
    ids = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValueError("Invalid ID parameter format")
        try:
            num = int(str(v).strip())
        except (TypeError, ValueError):
            raise ValueError("Invalid ID parameter format")
        if num <= 0:
            raise ValueError("IDs must be positive integers")
        ids.append(num)
    if not ids:
        raise ValueError("Empty id parameter")
    return ids


ERROR_CODE_MAP = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    500: "internal_error",
}


def error_response(exception, message, code=500):
    """标准错误响应格式 - 普通 JSON（不向客户端泄露内部异常细节）

    P1：payload.error.code 返回语义化错误码字符串（OpenAPI 契约），
    HTTP 状态码通过 exception.status_code 由调用方传递；原始数字码
    保留在 error.http_status 中便于调试。
    """
    payload = {
        "success": False,
        "error": {
            "code": ERROR_CODE_MAP.get(code, "internal_error"),
            "http_status": code,
            "message": message,
            "type": type(exception).__name__,
        },
        "timestamp": date_utils.utcnow().isoformat(),
    }
    _logger.error("REST API error (%s): %s", code, exception)
    # 仅对系统管理员附带调试详情，便于排障且不对外泄露 SQL/堆栈等内部信息
    try:
        if request.env and request.env.user.has_group("base.group_system"):
            payload["error"]["details"] = str(exception)
    except Exception:
        pass
    return payload
  
  
def success_response(data=None, message="Success", code=200):  
    """标准成功响应格式 - 普通 JSON"""  
    response = {  
        "success": True,  
        "message": message,  
        "timestamp": date_utils.utcnow().isoformat()  
    }  
    if data is not None:  
        response["data"] = data  
    return response  
  
  
class OdooAPI(http.Controller):  
    """Enhanced REST API Controller for Odoo 18 - 普通 JSON 格式"""  
  
    def get_request_data(self):  
        """安全获取 JSON 请求数据"""  
        try:  
            if hasattr(request, 'jsonrequest') and request.jsonrequest:  
                return request.jsonrequest  
            return request.get_json_data()  
        except (ValueError, json.JSONDecodeError) as e:  
            raise ValidationError("Invalid JSON data in request body")  
  
    def get_request_params(self):
        """获取 HTTP 参数"""
        return request.get_http_params()

    def validate_model_access(self, model_name):
        """验证模型访问权限"""
        try:
            model = request.env[model_name]
            model.check_access_rights("read")
            return model
        except (KeyError, AccessError) as e:
            raise ValidationError(f"Access denied to model '{model_name}'")

    # 记录/模型上可通过 REST 调用的公共方法白名单。
    # 采用"默认拒绝"策略：不在白名单内的方法一律禁止调用，
    # 避免任意 getattr 带来的远程代码执行风险（如 write/create/unlink/
    # sudo/with_user/browse/with_context/fields_get 以及 _execute 等
    # cr/call_kw 链路的危险入口）。
    ALLOWED_METHODS = {
        # 动作类公共方法（业务状态流转）
        "action_confirm",
        "action_done",
        "action_cancel",
        "action_draft",
        "button_confirm",
        "button_done",
        "button_cancel",
        "validate",
        "invalidate_cache",
        # 查询辅助类公共方法（只读、无副作用）
        "get_records",
        "name_search",
        "read_group",
    }

    def validate_method_call(self, model_obj, function_name):
        """验证方法调用安全性（白名单 + 存在性校验，默认拒绝）"""
        if not function_name or function_name.startswith("_"):
            raise HTTPBadRequest("Cannot call private methods")

        if function_name not in self.ALLOWED_METHODS:
            raise HTTPForbidden(f"Method '{function_name}' is not allowed via REST API")

        method = getattr(model_obj, function_name, None)
        if method is None or not callable(method):
            raise HTTPNotFound(f"Method '{function_name}' not found on model")

    def validate_query_fields(self, query, model_obj):
        """拒绝通过 REST 查询过滤器直接访问敏感字段。

        记录级可见性完全交由 Odoo 的 ACL/记录规则控制；这里只阻止
        利用 filter/domain 对密码、token 等敏感列进行探测或爆破。
        """
        try:
            parsed = Parser((query or "{*}").strip()).get_parsed()
        except Exception:
            return  # 语法错误稍后由 Serializer 统一抛出 400
        names = set()
        for field in parsed.get("include") or []:
            if isinstance(field, str):
                names.add(field)
        for field in parsed.get("exclude") or []:
            if isinstance(field, str):
                names.add(field)
        for name in names:
            if is_sensitive_field(name):
                raise HTTPForbidden(
                    f"Querying sensitive field '{name}' is not allowed"
                )

    def ensure_record_access(self, record, operation="read"):
        """记录级权限校验：确认当前用户对该记录有指定操作的权限。

        browse(id).exists() 只检查存在性，不校验记录规则；这里通过带
        limit=1 的 search 强制走完整 ACL/record-rule 流程，防止越权
        访问/操作不可见记录。

        S1：写/删操作必须以 "write"/"unlink" 模式调用——Odoo 的
        _access_rule_check(mode) 会按 mode 映射到对应 ir.rule 的
        global_write/unlink 域；只查 read 可见性会让"读得到但改不得"
        的记录绕过写/删记录规则。校验失败统一抛 404（与不可见记录
        同语义），不向客户端泄露存在性与权限差异。
        """
        if operation not in ("read", "write", "unlink", "create"):
            raise HTTPBadRequest(f"Invalid access operation '{operation}'")
        # 先走模型级 ACL（raise AccessError -> 由调用方转 403）
        record.check_access_rights(operation)
        try:
            # 再走记录规则：search 会自动应用当前 mode 对应的 record rules
            visible = record.search([("id", "in", record.ids)], limit=1)
        except AccessError as e:
            raise HTTPNotFound("Record not found") from None
        if not visible:
            raise HTTPNotFound("Record not found")
        # 对 write/unlink 显式触发规则校验（search 仅保证读可见性时，
        # Odoo 17+ 的 search 不校验写规则；_access_rule_check 才按 mode
        # 应用 global_write/global_unlink 域）。
        if operation != "read":
            try:
                record._access_rule_check(operation)
            except AccessError as e:
                raise HTTPNotFound("Record not found") from None
            except AttributeError:
                # 兼容旧版本方法名 _check_access_rules / 无该内部方法的场景：
                # 回退为不带 sudo 的按 id 搜索（仍受公司规则等约束）
                pass
        return True

    def jsonable(self, value):
        """将方法返回值转换为可 JSON 序列化的结构（recall Recordset 等）"""
        try:
            from odoo import models as odoo_models
            if isinstance(value, odoo_models.BaseModel):
                return value.ids
        except Exception:
            pass
        if isinstance(value, (list, tuple)):
            return [self.jsonable(v) for v in value]
        if isinstance(value, dict):
            return {str(k): self.jsonable(v) for k, v in value.items()}
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        return value

    def make_json_response(self, data, status=200):
        """创建标准 JSON 响应"""
        return request.make_json_response(data, status=status)

    # ---- S4: context 注入防护 -------------------------------------------
    # 仅允许这些键从请求体透传给 with_context；其余键（如 sudo 相关、
    # skip_ 开关、company 切换等）一律剔除并记日志，防止调用方通过
    # context 影响业务逻辑分支或绕过约束。default_* 前缀单独放行。
    ALLOWED_CONTEXT_KEYS = {
        "lang", "tz", "timezone", "uid", "allowed_company",
        "bin_size", "use_email_template", "from_module",
    }

    def sanitize_context(self, context):
        """过滤请求体中的 context，只保留白名单键（S4）。"""
        if not isinstance(context, dict):
            raise HTTPBadRequest("'context' must be a JSON object")
        safe = {}
        dropped = []
        for key, value in context.items():
            if (key in self.ALLOWED_CONTEXT_KEYS
                    or (isinstance(key, str) and key.startswith("default_"))):
                safe[key] = value
            else:
                dropped.append(key)
        if dropped:
            _logger.warning(
                "REST API: dropped disallowed context keys %s (user=%s)",
                sorted(dropped), request.env.uid if request.env else "?",
            )
        return safe

    # ---- I1: Idempotency-Key 支持 ---------------------------------------
    IDEMPOTENCY_KEY_RE = None  # 延迟编译

    def get_idempotency_key(self):
        """读取并校验 Idempotency-Key 请求头；无头返回 None。"""
        import re
        if OdooAPI.IDEMPOTENCY_KEY_RE is None:
            OdooAPI.IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")
        raw = request.httprequest.headers.get("Idempotency-Key")
        if raw is None:
            return None
        key = raw.strip()
        if not self.IDEMPOTENCY_KEY_RE.match(key):
            raise HTTPBadRequest(
                "Invalid Idempotency-Key (must be 8-200 chars of [A-Za-z0-9._:-], "
                "UUIDv4 recommended)")
        return key

    def idempotent_execute(self, payload, execute_fn):
        """在 Idempotency-Key 保护下执行写操作（I1）。

        payload: 参与作用域哈希的请求体（data/context 等）。
        execute_fn: 无参可调用，执行业务并返回 (response_payload_dict, status)。
        未携带 key 时直接透传执行；重放返回缓存副本并附 X-Idempotent-Replay 头。
        """
        key = self.get_idempotency_key()
        if not key:
            return self.make_json_response(*execute_fn())
        path = f"{key}|{request.httprequest.path}"
        method = request.httprequest.method
        store = request.env["ir.rest.api.idempotency"]
        record, state = store.find_or_reserve(path, method, payload)
        if state == "replay":
            cached = json.loads(record.response_json)
            resp = self.make_json_response(cached, status=record.http_status or 200)
            resp.headers["X-Idempotent-Replay"] = "true"
            return resp
        if state in ("conflict", "pending"):
            message = (
                "Idempotency-Key already used with a different request body"
                if state == "conflict"
                else "A request with this Idempotency-Key is still being processed; "
                     "retry shortly")
            exc = Exception(message)
            exc.status_code = 409
            return self.make_json_response(
                error_response(exc, message, 409), status=409)
        # fresh：执行业务；失败则删除占位行，允许客户端用同一 key 安全重试
        try:
            body, status = execute_fn()
        except Exception:
            record.sudo().unlink()
            raise
        if 200 <= status < 300:
            record.save_response(body, status)
        else:
            record.sudo().unlink()
        return self.make_json_response(body, status=status)

    def safe_json_loads(self, data, field_name="data"):
        """安全解析 JSON 数据"""
        try:
            return json.loads(data) if isinstance(data, str) else data
        except json.JSONDecodeError:
            raise ValidationError(f"Invalid JSON format in {field_name}")

    # 调用模型方法
    @http.route(
        "/api/v1/object/<string:model>/<string:function>",
        type="rest",
        auth="key",
        methods=["POST"],
        csrf=False,
    )
    def call_model_function(self, model, function):
        try:
            model_obj = self.validate_model_access(model)
            self.validate_method_call(model_obj, function)

            data = self.get_request_data()
            args = data.get("args", [])
            kwargs = data.get("kwargs", {})

            if not isinstance(args, list):
                raise HTTPBadRequest("'args' must be a JSON array")
            if not isinstance(kwargs, dict):
                raise HTTPBadRequest("'kwargs' must be a JSON object")

            method = getattr(model_obj, function)
            result = method(*args, **kwargs)

            return self.make_json_response(
                success_response(self.jsonable(result), "Method executed successfully")
            )

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 调用记录方法
    @http.route(
        "/api/v1/object/<string:model>/<int:rec_id>/<string:function>",
        type="rest",
        auth="key",
        methods=["POST"],
        csrf=False,
    )
    def call_record_function(self, model, rec_id, function, **post):
        try:
            model_obj = self.validate_model_access(model)
            self.validate_method_call(model_obj, function)

            data = self.get_request_data()
            args = data.get("args", [])
            kwargs = data.get("kwargs", {})

            if not isinstance(args, list):
                raise HTTPBadRequest("'args' must be a JSON array")
            if not isinstance(kwargs, dict):
                raise HTTPBadRequest("'kwargs' must be a JSON object")

            record = model_obj.browse(rec_id)
            if not record.exists():
                return self.make_json_response(
                    error_response(Exception("Not Found"), "Record not found", 404),
                    status=404,
                )
            # 记录级 ACL：确保当前用户确实可见该记录
            self.ensure_record_access(record)

            method = getattr(record, function)
            result = method(*args, **kwargs)

            return self.make_json_response(
                success_response(self.jsonable(result), "Method executed successfully")
            )

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 获取所有记录
    @http.route(
        "/api/v1/<string:model>",
        type="rest",
        auth="key",
        methods=["GET"],
        csrf=False,
    )
    def get_all_records(self, model):
        try:
            model_obj = self.validate_model_access(model)
            params = self.get_request_params()

            _logger.debug("Request params: %s", params)

            query = params.get("query", "{*}")
            # 防御性校验：拒绝查询过滤器访问敏感字段（记录级数据由 ACL/规则控制）
            self.validate_query_fields(query, model_obj)
            limit = min(int(params.get("limit", 80)), 1000)

            filters = []
            if "filter" in params:
                filters = self.safe_json_loads(params["filter"], "filter")
                validate_domain_fields(filters)  # S2

            orders = ""
            if "order" in params:
                orders = self.safe_json_loads(params["order"], "order")

            page_size = None
            current_page = 1
            offset = 0

            if "page_size" in params:
                page_size = min(int(params.get("page_size", 80)), 1000)
                current_page = max(int(params.get("page", 1)), 1)
                offset = page_size * (current_page - 1)

            # P1：limit 与 page_size 语义统一——显式传入 page_size 时，
            # 每页大小以 page_size 为准，避免 search(limit=...) 返回条数与
            # pagination.total_pages/next_page 相互矛盾；limit 仅在未分页时生效。
            effective_limit = page_size if page_size else limit

            record_count = model_obj.search_count(filters)

            if page_size:
                total_pages = math.ceil(record_count / page_size)
                prev_page = current_page - 1 if current_page > 1 else None
                next_page = current_page + 1 if current_page < total_pages else None
            else:
                total_pages = 1
                prev_page = None
                next_page = None

            records = model_obj.search(
                filters, offset=offset, order=orders, limit=effective_limit
            )

            serializer = Serializer(records, query, many=True)
            data = serializer.data

            response_data = {
                "records": data,
                "pagination": {
                    "count": len(records),
                    "total_count": record_count,
                    "current_page": current_page,
                    "total_pages": total_pages,
                    "page_size": page_size,
                    "prev_page": prev_page,
                    "next_page": next_page,
                },
            }

            return self.make_json_response(
                success_response(response_data, "Records retrieved successfully")
            )

        except (SyntaxError, QueryFormatError, LookupError) as e:
            # P1：query 引用了模型上不存在的字段时 Serializer 抛 LookupError，
            # 属于客户端请求错误，返回 400 而非 500
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 获取单个记录
    @http.route(
        "/api/v1/<string:model>/<int:rec_id>",
        type="rest",
        auth="key",
        methods=["GET"],
        csrf=False,
    )
    def get_one_record(self, model, rec_id, **params):
        try:
            model_obj = self.validate_model_access(model)
            # P1：query 参数同时兼容 fields 别名；单条记录直接 browse 后交给
            # Serializer 按查询裁剪字段，不再用 search_read 全字段读取
            # （旧实现完全忽略了 query 的字段选择）。
            query = params.get("query") or params.get("fields") or "{*}"
            self.validate_query_fields(query, model_obj)

            record = model_obj.browse(rec_id)
            if not record.exists():
                return self.make_json_response(
                    error_response(Exception("Not Found"), "Record not found", 404),
                    status=404,
                )
            # 记录级 ACL：不可见记录一律视为不存在
            self.ensure_record_access(record)

            serializer = Serializer(record, query, many=False)
            data = serializer.data

            return self.make_json_response(
                success_response(data, "Record retrieved successfully")
            )

        except (SyntaxError, QueryFormatError, LookupError) as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 创建记录
    @http.route(
        "/api/v1/<string:model>/",
        type="rest",
        auth="key",
        methods=["POST"],
        csrf=False,
    )
    def create_record(self, model, **post):
        try:
            model_obj = self.validate_model_access(model)
            model_obj.check_access_rights("create")

            if "data" not in post:
                raise ValidationError("Missing 'data' parameter in request body")

            data = post["data"]
            if isinstance(data, str):
                data = self.safe_json_loads(data, "data")

            context = {}
            if "context" in post:
                context = post["context"]
                if isinstance(context, str):
                    context = self.safe_json_loads(context, "context")
            context = self.sanitize_context(context or {})  # S4

            def _do_create():
                target = model_obj.with_context(**context) if context else model_obj
                record = target.create(data)
                return success_response(
                    record.read()[0], "Record created successfully"), 200

            # I1：携带 Idempotency-Key 时防重放（重试不产生重复记录）
            return self.idempotent_execute({"data": data, "context": context}, _do_create)

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 更新单个记录
    @http.route(
        "/api/v1/<string:model>/<int:rec_id>/",
        type="rest",
        auth="key",
        methods=["PUT", "PATCH"],
        csrf=False,
    )
    def update_record(self, model, rec_id, **post):
        """更新单条记录。

        I3：PUT 与 PATCH 均为部分更新（partial-update）语义——只覆盖
        data 中出现的字段，未出现的字段保持不变；不实现 RFC 7386 的
        全量替换。重复发送同一请求结果一致（天然幂等）。
        """
        try:
            model_obj = self.validate_model_access(model)
            model_obj.check_access_rights("write")

            if "data" not in post:
                raise ValidationError("Missing 'data' parameter in request body")

            data = post["data"]
            if isinstance(data, str):
                data = self.safe_json_loads(data, "data")

            context = {}
            if "context" in post:
                context = post["context"]
                if isinstance(context, str):
                    context = self.safe_json_loads(context, "context")
            context = self.sanitize_context(context or {})  # S4

            if context:
                rec = model_obj.with_context(**context).browse(rec_id)
            else:
                rec = model_obj.browse(rec_id)

            if not rec.exists():
                return self.make_json_response(
                    error_response(Exception("Not Found"), "Record not found", 404),
                    status=404,
                )
            # 记录级 ACL（S1）：写操作必须通过 write 模式的记录规则校验
            self.ensure_record_access(rec, operation="write")

            # 处理关系字段操作
            processed_data = {}  
            for field, value in data.items():  
                if isinstance(value, dict):  
                    operations = []  
                    for operation, ids in value.items():  
                        if operation == "create":  
                            # 创建新记录并链接  
                            operations.extend(Command.create(record_data) for record_data in ids)  
                        elif operation == "link":  
                            # 链接现有记录  
                            operations.extend(Command.link(id_) for id_ in ids)  
                        elif operation == "unlink":  
                            # 取消链接记录  
                            operations.extend(Command.unlink(id_) for id_ in ids)  
                        elif operation == "delete":  
                            # 删除记录  
                            operations.extend(Command.delete(id_) for id_ in ids)  
                        elif operation == "update":  
                            # 更新记录  
                            for update_data in ids:  
                                operations.append(Command.update(update_data['id'], update_data['values']))  
                        elif operation == "set":  
                            # 替换所有记录  
                            operations.append(Command.set(ids))  
                        elif operation == "clear":  
                            # 清除所有记录  
                            operations.append(Command.clear())  
                    processed_data[field] = operations  
                elif isinstance(value, list):  
                    # 直接设置记录列表  
                    processed_data[field] = Command.set(value)  
                else:  
                    processed_data[field] = value

            result = rec.write(processed_data)

            return self.make_json_response(
                success_response(rec.read()[0], "Record updated successfully")
            )

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 批量更新记录
    @http.route(
        "/api/v1/<string:model>/",
        type="rest",
        auth="key",
        methods=["PUT", "PATCH"],
        csrf=False,
    )
    def update_records(self, model, **post):
        """批量更新（PUT/PATCH 同义，部分字段更新，见 update_record 说明）。"""
        try:
            model_obj = self.validate_model_access(model)
            model_obj.check_access_rights("write")

            params = self.get_request_params()

            if "data" not in post:
                raise ValidationError("Missing 'data' parameter in request body")

            data = post["data"]
            if isinstance(data, str):
                data = self.safe_json_loads(data, "data")

            filters = []
            if "filter" in post:
                filters = post["filter"]
                if isinstance(filters, str):
                    filters = self.safe_json_loads(filters, "filter")
                validate_domain_fields(filters)  # S2
            # P1：批量更新的 id 允许来自 JSON body（ids/id）或 query 参数，
            # DELETE/PATCH 携带 body 时同样可用；解析统一走 parse_ids，
            # 不再使用 ast.literal_eval。
            raw_ids = post.get("ids") or post.get("id") or params.get("id")
            if not filters and raw_ids is not None:
                try:
                    rec_ids = parse_ids(raw_ids)
                except ValueError as e:
                    raise ValidationError(str(e))
                filters = [("id", "in", rec_ids)]

            if not filters:
                raise ValidationError("Missing filter or id parameter")

            context = {}
            if "context" in post:
                context = post["context"]
                if isinstance(context, str):
                    context = self.safe_json_loads(context, "context")
            context = self.sanitize_context(context or {})  # S4

            # 获取记录
            if context:
                recs = model_obj.with_context(**context).search(filters)
            else:
                recs = model_obj.search(filters)

            # S1：批量更新前按 write 模式校验记录规则，
            # "读得到但改不得"的记录会被拒绝（404，不泄露差异）
            if recs:
                self.ensure_record_access(recs, operation="write")

            if not recs.exists():
                return self.make_json_response(
                    error_response(
                        Exception("Not Found"), "No records found to update", 404
                    ),
                    status=404,
                )

            # 处理关系字段操作
            processed_data = {}
            for field, value in data.items():
                if isinstance(value, dict):
                    operations = []
                    for operation, ids in value.items():
                        if operation == "create":  
                            # 创建新记录并链接  
                            operations.extend(Command.create(record_data) for record_data in ids)  
                        elif operation == "link":  
                            # 链接现有记录  
                            operations.extend(Command.link(id_) for id_ in ids)  
                        elif operation == "unlink":  
                            # 取消链接记录  
                            operations.extend(Command.unlink(id_) for id_ in ids)  
                        elif operation == "delete":  
                            # 删除记录  
                            operations.extend(Command.delete(id_) for id_ in ids)  
                        elif operation == "update":  
                            # 更新记录  
                            for update_data in ids:  
                                operations.append(Command.update(update_data['id'], update_data['values']))  
                        elif operation == "set":  
                            # 替换所有记录  
                            operations.append(Command.set(ids))  
                        elif operation == "clear":  
                            # 清除所有记录  
                            operations.append(Command.clear()) 
                    processed_data[field] = operations
                elif isinstance(value, list):
                    processed_data[field] = [(6, 0, value)]
                else:
                    processed_data[field] = value

            # 更新记录
            result = recs.write(processed_data)

            return self.make_json_response(
                success_response(
                    {"updated": len(recs), "success": result, "records": recs.read()},
                    "Records updated successfully",
                )
            )

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 删除单个记录
    @http.route(
        "/api/v1/<string:model>/<int:rec_id>/",
        type="rest",
        auth="key",
        methods=["DELETE"],
        csrf=False,
    )
    def delete_record(self, model, rec_id, **kw):
        try:
            model_obj = self.validate_model_access(model)
            model_obj.check_access_rights("unlink")

            record = model_obj.browse(rec_id)
            # I2：DELETE 幂等——目标不存在（含首次删除后的重试）统一返回
            # 200 {deleted: 0}，客户端可安全重放；不再区分 404。
            if not record.exists():
                return self.make_json_response(
                    success_response(
                        {"deleted": 0, "requested": 1, "id": rec_id},
                        "Nothing to delete (record already absent)",
                    )
                )
            # 记录级 ACL（S1）：删除操作必须通过 unlink 模式的记录规则校验
            self.ensure_record_access(record, operation="unlink")

            record.unlink()

            return self.make_json_response(
                success_response(
                    {"deleted": 1, "requested": 1, "id": rec_id},
                    "Record deleted successfully",
                )
            )

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 删除多个记录
    @http.route(
        "/api/v1/<string:model>/",
        type="rest",
        auth="key",
        methods=["DELETE"],
        csrf=False,
    )
    def delete_records(self, model, **kw):
        try:
            model_obj = self.validate_model_access(model)
            model_obj.check_access_rights("unlink")

            params = self.get_request_params()

            # P1：id 可来自 JSON body（DELETE 携带 payload）或 query 参数；
            # 单个整数、逗号分隔、JSON 数组均可，统一由 parse_ids 校验。
            raw_ids = kw.get("ids") or kw.get("id") or params.get("id")
            if raw_ids is None:
                raise ValidationError("Missing 'id' parameter")
            try:
                rec_ids = parse_ids(raw_ids)
            except ValueError as e:
                raise ValidationError(str(e))

            # S1：search 走读规则；再对命中集合显式校验 unlink 记录规则
            records = model_obj.search([("id", "in", rec_ids)])
            if records:
                self.ensure_record_access(records, operation="unlink")

            # I2：批量删除同样幂等——无可删记录时返回 200 deleted=0
            record_count = len(records)
            if record_count:
                records.unlink()

            return self.make_json_response(
                success_response(
                    {
                        "deleted": record_count,
                        "requested": len(rec_ids),
                        "message": f"Successfully deleted {record_count} records",
                    },
                    "Records deleted successfully",
                )
            )

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 获取文件/二进制字段
    @http.route(
        "/api/v1/<string:model>/<int:rec_id>/<string:field>",
        type="rest",
        auth="key",
        methods=["GET"],
        csrf=False,
    )
    def get_file(self, model, rec_id, field, **post):
        try:
            model_obj = self.validate_model_access(model)

            # P0：禁止通过文件端点读取敏感字段（如 password / token 类）
            if is_sensitive_field(field):
                raise HTTPForbidden(
                    f"Reading sensitive field '{field}' is not allowed"
                )

            record = model_obj.browse(rec_id)
            if not record.exists():
                return self.make_json_response(
                    error_response(Exception("Not Found"), "Record not found", 404),
                    status=404,
                )
            # 记录级 ACL：不可见记录一律视为不存在
            self.ensure_record_access(record)

            if field not in model_obj._fields:
                raise ValidationError(
                    f"Field '{field}' does not exist in model '{model}'"
                )

            field_value = getattr(record, field, None)

            if not field_value:
                return self.make_json_response(
                    error_response(
                        Exception("Not Found"), "File not found or field is empty", 404
                    ),
                    status=404,
                )

            field_type = model_obj._fields[field].type
            if field_type == "binary":
                try:
                    import base64

                    file_data = base64.b64decode(field_value)
                    return http.Response(
                        file_data, status=200, mimetype="application/octet-stream"
                    )
                except Exception:
                    return http.Response(field_value, status=200, mimetype="text/plain")
            else:
                return http.Response(
                    str(field_value), status=200, mimetype="text/plain"
                )

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 批量创建记录
    @http.route(
        "/api/v1/<string:model>/batch",
        type="rest",
        auth="key",
        methods=["POST"],
        csrf=False,
    )
    def create_records_batch(self, model, **post):
        try:
            model_obj = self.validate_model_access(model)
            model_obj.check_access_rights("create")

            if "data" not in post:
                raise ValidationError("Missing 'data' parameter in request body")

            data = post["data"]
            if isinstance(data, str):
                data = self.safe_json_loads(data, "data")

            if not isinstance(data, list):
                raise ValidationError("Data must be a list for batch creation")

            context = {}
            if "context" in post:
                context = post["context"]
                if isinstance(context, str):
                    context = self.safe_json_loads(context, "context")
            context = self.sanitize_context(context or {})  # S4

            def _do_batch():
                target = model_obj.with_context(**context) if context else model_obj
                records = target.create(data)
                return success_response(
                    {"created": len(records), "records": records.read()},
                    "Records created successfully",
                ), 200

            # I1：整个批量请求作为一个幂等单元，重放不会二次插入
            return self.idempotent_execute({"data": data, "context": context}, _do_batch)

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 健康检查端点
    @http.route(
        "/api/v1/health",
        type="rest",
        auth="none",
        methods=["GET"],
        csrf=False,
    )
    def health_check(self):
        """简单的健康检查端点"""
        return self.make_json_response(
            success_response(
                {"status": "healthy", "version": API_VERSION,
                 "odoo_version": release.version},
                "API is healthy",
            )
        )

    # API 信息端点
    @http.route(
        "/api/v1/info",
        type="rest",
        auth="key",
        methods=["GET"],
        csrf=False,
    )
    def api_info(self):  
        """API 信息端点"""  
        return self.make_json_response(  
            success_response(  
                {  
                    "api_version": API_VERSION,  
                    "odoo_version": release.version,  
                    "odoo_series": release.series,  
                    "supported_methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],  
                    "authentication": "Bearer token required",  
                    "endpoints": {  
                        "models": "/api/v1/<model>",  
                        "records": "/api/v1/<model>/<id>",  
                        "functions": "/api/v1/object/<model>/<function>",  
                        "record_functions": "/api/v1/object/<model>/<id>/<function>",  
                        "files": "/api/v1/<model>/<id>/<field>",  
                        "batch_create": "/api/v1/<model>/batch",  
                        "health": "/api/v1/health",  
                    },  
                },  
                "API information",  
            )  
        )

    # 搜索记录（高级搜索）
    @http.route(
        "/api/v1/<string:model>/search",
        type="rest",
        auth="key",
        methods=["POST"],
        csrf=False,
    )
    def search_records(self, model, **post):
        try:
            model_obj = self.validate_model_access(model)

            data = self.get_request_data()

            # 获取搜索参数
            domain = data.get("domain", [])
            validate_domain_fields(domain)  # S2
            fields = data.get("fields", [])
            for fname in fields or []:
                if isinstance(fname, str) and is_sensitive_field(fname):
                    raise HTTPForbidden(
                        f"Reading sensitive field '{fname}' is not allowed"
                    )
            limit = min(int(data.get("limit", 80)), 1000)
            offset = int(data.get("offset", 0))
            order = data.get("order", "")

            # 执行搜索
            if fields:
                records = model_obj.search_read(
                    domain, fields=fields, limit=limit, offset=offset, order=order
                )
                return self.make_json_response(
                    success_response(
                        {"records": records, "count": len(records)},
                        "Search completed successfully",
                    )
                )
            else:
                records = model_obj.search(
                    domain, limit=limit, offset=offset, order=order
                )
                return self.make_json_response(
                    success_response(
                        {"records": records.read(), "count": len(records)},
                        "Search completed successfully",
                    )
                )

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # 获取模型字段信息
    @http.route(
        "/api/v1/<string:model>/fields",
        type="rest",
        auth="key",
        methods=["GET"],
        csrf=False,
    )
    def get_model_fields(self, model):
        try:
            model_obj = self.validate_model_access(model)

            fields_info = model_obj.fields_get()
            # S2：字段元数据同样不得暴露敏感字段定义（含 ttype/帮助文本等）
            for fname in list(fields_info):
                if is_sensitive_field(fname):
                    del fields_info[fname]

            return self.make_json_response(
                success_response(
                    {"model": model, "fields": fields_info},
                    "Model fields retrieved successfully",
                )
            )

        except HTTPException as e:
            # P1：HTTPBadRequest/Forbidden/NotFound 按真实状态码返回，
            # 不再被下方宽 handlers 吞成 500/400
            return self.make_json_response(
                error_response(e, e.description, e.code or 500), status=e.code or 500
            )
        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )

    # OpenAPI 文档端点（S3）：默认公开；系统参数 rest_api.openapi_public
    # 设为 False 后仅系统管理员可访问，其他用户/游客得到 403。
    @http.route(
        "/api/v1/openapi.json",
        type="rest",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def openapi_spec(self):
        public = True
        try:
            param = request.env["ir.config_parameter"].sudo().get_param(
                "rest_api.openapi_public", "True"
            )
            public = str(param).lower() not in ("false", "0", "off", "no")
        except Exception:
            public = True
        if not public:
            try:
                is_admin = request.env.user and request.env.user.has_group(
                    "base.group_system"
                )
            except Exception:
                is_admin = False
            if not is_admin:
                exc = HTTPForbidden(
                    "OpenAPI document requires administrator access "
                    "(system parameter rest_api.openapi_public)")
                return self.make_json_response(
                    error_response(exc, exc.description, 403), status=403
                )
        spec = build_openapi_spec()
        return http.Response(json.dumps(spec), mimetype="application/json", status=200)
