class ApiError(Exception):
    def __init__(self, status, code, message=None, details=None):
        """Describe a failure the apps can branch on.
        code is the stable string the app matches; message is for developers.
        details carries structured extras such as a booking id."""
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ").capitalize() + "."
        self.details = details or {}


def bad_request(code, message=None, details=None):
    """Build a 400 for a malformed request.
    The body or query could not be understood at all.
    Distinct from 422, which is well formed but refused by a rule."""
    return ApiError(400, code, message, details)


def not_found(code="NOT_FOUND", message=None):
    """Build a 404 for something missing or not the caller's.
    Somebody else's resource is 404, never 403, so existence is not leaked.
    The default code is NOT_FOUND."""
    return ApiError(404, code, message or "Not found.")


def conflict(code, message=None, details=None):
    """Build a 409 for a conflict with the current state.
    Used when the thing exists but has moved on.
    details may carry where the caller should go instead."""
    return ApiError(409, code, message, details)


def unprocessable(code, message=None, details=None):
    """Build a 422 for a well formed request refused by a rule.
    Each distinct rule gets its own code so the app can word it.
    details names the field or question at fault."""
    return ApiError(422, code, message, details)
