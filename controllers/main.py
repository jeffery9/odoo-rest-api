# -*- coding: utf-8 -*-
import json
import math
import logging
import requests
import ast

from odoo import http, exceptions, _
from odoo.http import request, HttpDispatcher, Request

from .serializers import Serializer
from .exceptions import QueryFormatError

_logger = logging.getLogger(__name__)


def error_response(
    exception,
    message,
    code=200,
):
    return {
        "code": code,
        "message": message,
        "data": {
            "name": str(exception),
            "debug": "",
            "message": message,
            "arguments": list(exception.args),
            "exception_type": type(exception).__name__,
        },
    }


def not_found(
    message="Not found.",
    code=404,
):
    return {
        "code": code,
        "message": message,
    }


class OdooAPI(http.Controller):
    # call model function
    @http.route(
        "/api/v1/object/<string:model>/<string:function>",
        type="http",
        auth="key",
        methods=["POST"],
        csrf=False,
    )
    def call_model_function(self, model, function):
        params = request.get_http_params()
        data = request.get_json_data()

        try:
            args = data.get("args")
            kwargs = data.get("kwargs")

            if function.startswith("_"):
                raise UserWarning("Can't call private method.")

            if args and kwargs:
                result = getattr(request.env[model], function)(*args, **kwargs)
            elif args and not kwargs:
                result = getattr(request.env[model], function)(*args)
            elif not args and kwargs:
                result = getattr(request.env[model], function)(**kwargs)
            else:
                result = getattr(request.env[model], function)()

            if result:
                return request.make_json_response(result, status=200)
            else:
                return http.Response("Fail", status=500, mimetype="application/json")

        except Exception as e:
            res = error_response(e, "Exception", code=500)
            return http.Response(
                json.dumps(res), status=500, mimetype="application/json"
            )

    # call record function
    @http.route(
        "/api/v1/object/<string:model>/<int:rec_id>/<string:function>",
        type="http",
        auth="key",
        methods=["POST"],
        csrf=False,
    )
    def call_record_function(self, model, rec_id, function, **post):
        params = request.get_http_params()
        data = request.get_json_data()

        try:
            args = data.get("args")
            kwargs = data.get("kwargs")

            if function.startswith("_"):
                raise UserWarning("Can't call private method.")

            record = request.env[model].browse(rec_id)

            if args and kwargs:
                result = getattr(record, function)(*args, **kwargs)
            elif args and not kwargs:
                result = getattr(record, function)(*args)
            elif not args and kwargs:
                result = getattr(record, function)(**kwargs)
            else:
                result = getattr(record, function)()

            if result:
                return request.make_json_response(result, status=200)
            else:
                return http.Response("Fail", status=500, mimetype="application/json")

        except Exception as e:
            res = error_response(e, "Exception", code=500)
            return http.Response(
                json.dumps(res), status=500, mimetype="application/json"
            )

    # TODO make this to works
    # get all records
    @http.route(
        "/api/v1/<string:model>",
        type="http",
        auth="key",
        methods=["GET"],
        csrf=False,
    )
    def get_all_records(self, model):
        try:
            records = request.env[model]
            params = request.get_http_params()

            _logger.warning(params)

            if "query" in params:
                query = params["query"]
            else:
                query = "{*}"

            if "order" in params:
                orders = json.loads(params["order"])
            else:
                orders = ""

            if "filter" in params:
                filters = json.loads(params["filter"])
                records = request.env[model].search(filters, order=orders)

            prev_page = None
            next_page = None
            total_page_number = 1
            current_page = 1

            if "page_size" in params:
                page_size = int(params["page_size"])
                count = len(records)
                total_page_number = math.ceil(count / page_size)

                if "page" in params:
                    current_page = int(params["page"])
                else:
                    current_page = 1  # Default page Number
                start = page_size * (current_page - 1)
                stop = current_page * page_size
                records = records[start:stop]
                next_page = (
                    current_page + 1
                    if 0 < current_page + 1 <= total_page_number
                    else None
                )
                prev_page = (
                    current_page - 1
                    if 0 < current_page - 1 <= total_page_number
                    else None
                )

            if "limit" in params:
                limit = int(params["limit"])
                records = records[0:limit]

                serializer = Serializer(records, query, many=True)
                data = serializer.data

        except (SyntaxError, QueryFormatError) as e:
            res = error_response(e, e.msg)
            return http.Response(
                json.dumps(res), status=200, mimetype="application/json"
            )

        res = {
            "count": len(records),
            "prev": prev_page,
            "current": current_page,
            "next": next_page,
            "total_pages": total_page_number,
            "result": data,
        }
        return http.Response(json.dumps(res), status=200, mimetype="application/json")

    # get one record
    @http.route(
        "/api/v1/<string:model>/<int:rec_id>",
        type="http",
        auth="key",
        methods=["GET"],
        csrf=False,
    )
    def get_one_record(self, model, rec_id, **params):
        try:
            # all_fields = (
            #     request.env["ir.model"].search([("model", "=", model)]).fields_get()
            # )
            # _logger.warning(all_fields)

            data = (
                request.env[model]
                .with_context(active_test=True)
                .search_read([("id", "=", rec_id)])
            )
            if data:
                return request.make_json_response(data)
            else:
                return request.make_json_response(not_found(), status=404)

        except Exception as e:
            res = error_response(e, "Error!", code=500)
            return request.make_json_response(res, status=500)

    # TODO make this to works
    # create record
    @http.route(
        "/api/v1/<string:model>/",
        type="http",
        auth="key",
        methods=["POST"],
        csrf=False,
    )
    def create_record(self, model, **post):
        try:
            data = post["data"]
            model_to_post = request.env[model]

            if "context" in post:
                context = post["context"]
                record = model_to_post.with_context(**context).create(data)
            else:
                record = model_to_post.create(data)

        except Exception as e:
            res = error_response(e, e.msg)
            return http.Response(
                json.dumps(res), status=500, mimetype="application/json"
            )

        return http.Response(
            json.dumps(record.read()), status=200, mimetype="application/json"
        )

    # TODO make this to works
    # update one record
    @http.route(
        "/api/v1/<string:model>/<int:rec_id>/",
        type="http",
        auth="key",
        methods=["PUT"],
        csrf=False,
    )
    def update_record(self, model, rec_id, **post):
        try:
            data = post["data"]

            model_to_put = request.env[model]

            if "context" in post:
                # TODO: Handle error raised by `ensure_one`
                rec = (
                    model_to_put.with_context(**post["context"])
                    .browse(rec_id)
                    .ensure_one()
                )
            else:
                rec = model_to_put.browse(rec_id).ensure_one()

            for field in data:
                if isinstance(data[field], dict):
                    operations = []
                    for operation in data[field]:
                        if operation == "push":
                            operations.extend(
                                (4, rec_id, _) for rec_id in data[field].get("push")
                            )
                        elif operation == "pop":
                            operations.extend(
                                (3, rec_id, _) for rec_id in data[field].get("pop")
                            )
                        elif operation == "delete":
                            operations.extend(
                                (2, rec_id, _) for rec_id in data[field].get("delete")
                            )
                        else:
                            data[field].pop(operation)  # Invalid operation

                    data[field] = operations
                elif isinstance(data[field], list):
                    data[field] = [(6, _, data[field])]  # Replace operation
                else:
                    pass

            result = rec.write(data)
        except Exception as e:
            res = error_response(e, e.msg)
            return http.Response(
                json.dumps(res), status=500, mimetype="application/json"
            )
        if result:
            return http.Response(
                json.dumps(rec.read()), status=201, mimetype="application/json"
            )
        else:
            return http.Response(
                json.dumps(rec.read()), status=202, mimetype="application/json"
            )

    # TODO make this to works
    # update multiple record
    @http.route(
        "/api/v1/<string:model>/", type="http", auth="key", methods=["PUT"], csrf=False
    )
    def update_records(self, model, **post):
        params = request.get_http_params()
        # id=[1,2,3]
        # eval('[1,2,3]') => [1,2,3]
        try:
            rec_ids = ast.literal_eval(params.get("id"))
            assert isinstance(rec_ids, list)

            data = post["data"]
        except KeyError:
            msg = "`data` parameter is not found on PUT request body"
            raise exceptions.ValidationError(msg)

        try:
            model_to_put = request.env[model]
        except KeyError:
            msg = "The model `%s` does not exist." % model
            raise exceptions.ValidationError(msg)

        # TODO: Handle errors on filter
        filters = post["filter"]

        if "context" in post:
            recs = model_to_put.with_context(**post["context"]).search(filters)
        else:
            recs = model_to_put.search(filters)

        # TODO: Handle data validation
        for field in data:
            if isinstance(data[field], dict):
                operations = []
                for operation in data[field]:
                    if operation == "push":
                        operations.extend(
                            (4, rec_id, _) for rec_id in data[field].get("push")
                        )
                    elif operation == "pop":
                        operations.extend(
                            (3, rec_id, _) for rec_id in data[field].get("pop")
                        )
                    elif operation == "delete":
                        operations.extend(
                            (2, rec_id, _) for rec_id in data[field].get("delete")
                        )
                    else:
                        pass  # Invalid operation

                data[field] = operations
            elif isinstance(data[field], list):
                data[field] = [(6, _, data[field])]  # Replace operation
            else:
                pass

        if recs.exists():
            try:
                return recs.write(data)
            except Exception as e:
                # TODO: Return error message(e.msg) on a response
                return False
        else:
            # No records to update
            return True

    # delete one record
    @http.route(
        "/api/v1/<string:model>/<int:rec_id>/",
        type="http",
        auth="key",
        methods=["DELETE"],
        csrf=False,
    )
    def delete_record(self, model, rec_id, **kw):
        try:
            result = request.env[model].search([("id", "=", rec_id)]).unlink()

            if result:
                return http.Response("Ok", status=200, mimetype="application/json")
            else:
                return http.Response("Fail", status=500, mimetype="application/json")

        except Exception as e:
            res = error_response(e, e.message)
            return http.Response(
                json.dumps(res), status=200, mimetype="application/json"
            )

    # delete multiple record
    @http.route(
        "/api/v1/<string:model>/",
        type="http",
        auth="key",
        methods=["DELETE"],
        csrf=False,
    )
    def delete_records(self, model, **kw):
        params = request.get_http_params()
        # id=[1,2,3]
        # eval('[1,3,4]')
        try:
            rec_ids = ast.literal_eval(params.get("id"))
            assert isinstance(rec_ids, list)

            result = request.env[model].search([("id", "in", rec_ids)]).unlink()

            if result:
                return http.Response("Ok", status=200, mimetype="application/json")
            else:
                return http.Response("Fail", status=500, mimetype="application/json")

        except Exception as e:
            res = error_response(e, str(e))
            return http.Response(
                json.dumps(res), status=200, mimetype="application/json"
            )

    # TODO make this to works
    # get file
    @http.route(
        "/api/v1/<string:model>/<int:rec_id>/<string:field>",
        type="http",
        auth="key",
        methods=["GET"],
        csrf=False,
    )
    def get_file(self, model, rec_id, field, **post):
        try:
            request.env[model]

            rec = request.env[model].browse(rec_id).ensure_one()
            if rec.exists():
                src = getattr(rec, field).decode("utf-8")
            else:
                src = False
            return http.Response(src)

        except KeyError as e:
            msg = "The model `%s` does not exist." % model
            res = error_response(e, msg)
            return http.Response(
                json.dumps(res), status=200, mimetype="application/json"
            )
