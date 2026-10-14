"""Authentication backend that signs users in by email, case-insensitively.

`USERNAME_FIELD` is `email`, and Postgres matches `email` case-sensitively, so
the stock ModelBackend rejects `Demo@Gmail.com` for a `demo@gmail.com` account
(SQLite happens to be lenient, which is why this only bites in production).
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class EmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD)
        if username is None or password is None:
            return None
        try:
            user = UserModel.objects.get(Q(**{f"{UserModel.USERNAME_FIELD}__iexact": username}))
        except UserModel.DoesNotExist:
            # Run the default backend so a bad email costs the same time as a
            # bad password (avoids a user-enumeration timing oracle).
            UserModel().set_password(password)
            return None
        except UserModel.MultipleObjectsReturned:
            user = UserModel.objects.filter(
                Q(**{f"{UserModel.USERNAME_FIELD}__iexact": username})
            ).first()
        if user is not None and user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
