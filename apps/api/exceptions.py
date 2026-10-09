from rest_framework.views import exception_handler


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = {
            "error": True,
            "message": _extract_message(response.data),
            "detail": response.data,
        }
    return response


def _extract_message(data):
    if isinstance(data, str):
        return data
    if isinstance(data, list) and data:
        return _extract_message(data[0])
    if isinstance(data, dict):
        for key in ("detail", "non_field_errors", "message"):
            if key in data:
                return _extract_message(data[key])
        return str(next(iter(data.values()), "An error occurred"))
    return "An error occurred"
