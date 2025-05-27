# -*- coding: utf-8 -*-  
import json  
import math  
import logging  
import ast  
import datetime  
from datetime import datetime  
  
from odoo import http, exceptions, _, release, Command  
from odoo.http import request, Dispatcher  
from odoo.exceptions import AccessError, AccessDenied, UserError, ValidationError  
  
from .serializers import Serializer  
from .exceptions import QueryFormatError  
  
_logger = logging.getLogger(__name__)  
  
  
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
        # Parse JSON body for POST/PUT requests  
        if request.httprequest.method in ('POST', 'PUT'):  
            try:  
                request.jsonrequest = json.loads(request.httprequest.get_data(as_text=True))  
            except (ValueError, json.JSONDecodeError):  
                request.jsonrequest = {}  
          
        # Set up params from URL args and query string  
        request.params = dict(request.get_http_params(), **args)  
          
        # Call the endpoint  
        if request.db:  
            return request.registry['ir.http']._dispatch(endpoint)  
        else:  
            return endpoint(**request.params)  
  
    def handle_error(self, exc):  
        """Handle errors for REST API"""  
        from werkzeug.exceptions import HTTPException  
        if isinstance(exc, HTTPException):  
            return exc  
        return http.Response(  
            json.dumps({"error": str(exc)}),  
            status=500,  
            mimetype="application/json"  
        )  
  
  
def error_response(exception, message, code=500):  
    """标准错误响应格式 - 普通 JSON"""  
    return {  
        "success": False,  
        "error": {  
            "code": code,  
            "message": message,  
            "type": type(exception).__name__,  
            "details": str(exception)  
        },  
        "timestamp": datetime.now().isoformat()  
    }  
  
  
def success_response(data=None, message="Success", code=200):  
    """标准成功响应格式 - 普通 JSON"""  
    response = {  
        "success": True,  
        "message": message,  
        "timestamp": datetime.now().isoformat()  
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

    def validate_method_call(self, function_name):
        """验证方法调用安全性"""
        if function_name.startswith("_"):
            raise ValidationError("Cannot call private methods")

        dangerous_methods = ["unlink", "sudo", "with_user"]
        if function_name in dangerous_methods:
            raise ValidationError(f"Method '{function_name}' is restricted")

    def make_json_response(self, data, status=200):
        """创建标准 JSON 响应"""
        return request.make_json_response(data, status=status)

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
            self.validate_method_call(function)

            data = self.get_request_data()
            args = data.get("args", [])
            kwargs = data.get("kwargs", {})

            if args and kwargs:
                result = getattr(model_obj, function)(*args, **kwargs)
            elif args and not kwargs:
                result = getattr(model_obj, function)(*args)
            elif not args and kwargs:
                result = getattr(model_obj, function)(**kwargs)
            else:
                result = getattr(model_obj, function)()

            return self.make_json_response(
                success_response(result, "Method executed successfully")
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
            self.validate_method_call(function)

            data = self.get_request_data()
            args = data.get("args", [])
            kwargs = data.get("kwargs", {})

            record = model_obj.browse(rec_id)
            if not record.exists():
                return self.make_json_response(
                    error_response(Exception("Not Found"), "Record not found", 404),
                    status=404,
                )

            if args and kwargs:
                result = getattr(record, function)(*args, **kwargs)
            elif args and not kwargs:
                result = getattr(record, function)(*args)
            elif not args and kwargs:
                result = getattr(record, function)(**kwargs)
            else:
                result = getattr(record, function)()

            return self.make_json_response(
                success_response(result, "Method executed successfully")
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
            limit = min(int(params.get("limit", 80)), 1000)

            filters = []
            if "filter" in params:
                filters = self.safe_json_loads(params["filter"], "filter")

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
                filters, offset=offset, order=orders, limit=limit
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

        except (SyntaxError, QueryFormatError) as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
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
            query = params.get("query", "{*}")

            records = model_obj.with_context(active_test=True).search_read(
                [("id", "=", rec_id)]
            )

            if not records:
                return self.make_json_response(
                    error_response(Exception("Not Found"), "Record not found", 404),
                    status=404,
                )

            serializer = Serializer(records, query, many=True)
            data = serializer.data

            return self.make_json_response(
                success_response(
                    data[0] if data else {}, "Record retrieved successfully"
                )
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

            if context:
                record = model_obj.with_context(**context).create(data)
            else:
                record = model_obj.create(data)

            return self.make_json_response(
                success_response(record.read()[0], "Record created successfully")
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
        methods=["PUT"],
        csrf=False,
    )
    def update_record(self, model, rec_id, **post):
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

            if context:
                rec = model_obj.with_context(**context).browse(rec_id)
            else:
                rec = model_obj.browse(rec_id)

            if not rec.exists():
                return self.make_json_response(
                    error_response(Exception("Not Found"), "Record not found", 404),
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
                    # 直接设置记录列表  
                    processed_data[field] = Command.set(value)  
                else:  
                    processed_data[field] = value

            result = rec.write(processed_data)

            return self.make_json_response(
                success_response(rec.read()[0], "Record updated successfully")
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
        methods=["PUT"],
        csrf=False,
    )
    def update_records(self, model, **post):
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
            elif "id" in params:
                try:
                    rec_ids = ast.literal_eval(params.get("id"))
                    if isinstance(rec_ids, list):
                        filters = [("id", "in", rec_ids)]
                    else:
                        raise ValidationError("Invalid id parameter format")
                except (ValueError, SyntaxError):
                    raise ValidationError("Invalid id parameter format")

            if not filters:
                raise ValidationError("Missing filter or id parameter")

            context = {}
            if "context" in post:
                context = post["context"]
                if isinstance(context, str):
                    context = self.safe_json_loads(context, "context")

            # 获取记录
            if context:
                recs = model_obj.with_context(**context).search(filters)
            else:
                recs = model_obj.search(filters)

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
            if not record.exists():
                return self.make_json_response(
                    error_response(Exception("Not Found"), "Record not found", 404),
                    status=404,
                )

            result = record.unlink()

            return self.make_json_response(
                success_response(
                    {"success": True, "message": "Record deleted successfully"},
                    "Record deleted successfully",
                )
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

            if "id" not in params:
                raise ValidationError("Missing 'id' parameter")

            try:
                rec_ids = ast.literal_eval(params.get("id"))
                if not isinstance(rec_ids, list):
                    raise ValidationError("ID parameter must be a list")
            except (ValueError, SyntaxError):
                raise ValidationError("Invalid ID parameter format")

            records = model_obj.search([("id", "in", rec_ids)])

            if not records.exists():
                return self.make_json_response(
                    error_response(
                        Exception("Not Found"), "No records found to delete", 404
                    ),
                    status=404,
                )

            record_count = len(records)
            result = records.unlink()

            return self.make_json_response(
                success_response(
                    {
                        "success": True,
                        "deleted": record_count,
                        "message": f"Successfully deleted {record_count} records",
                    },
                    "Records deleted successfully",
                )
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

            record = model_obj.browse(rec_id)
            if not record.exists():
                return self.make_json_response(
                    error_response(Exception("Not Found"), "Record not found", 404),
                    status=404,
                )

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

            if context:
                records = model_obj.with_context(**context).create(data)
            else:
                records = model_obj.create(data)

            return self.make_json_response(
                success_response(
                    {"created": len(records), "records": records.read()},
                    "Records created successfully",
                )
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
                {"status": "healthy", "version": "1.0", "odoo_version": "18.0"},
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
                    "api_version": "1.0",  
                    "odoo_version": release.version,  
                    "odoo_series": release.series,  
                    "supported_methods": ["GET", "POST", "PUT", "DELETE"],  
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
            fields = data.get("fields", [])
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

            return self.make_json_response(
                success_response(
                    {"model": model, "fields": fields_info},
                    "Model fields retrieved successfully",
                )
            )

        except ValidationError as e:
            return self.make_json_response(error_response(e, str(e), 400), status=400)
        except Exception as e:
            return self.make_json_response(
                error_response(e, "Internal server error", 500), status=500
            )
