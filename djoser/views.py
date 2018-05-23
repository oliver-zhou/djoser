from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.urls.exceptions import NoReverseMatch
from django.db.models import Q

from rest_framework import generics, permissions, status, views
from rest_framework.response import Response
from rest_framework.reverse import reverse

from djoser import utils, signals
from djoser.compat import get_user_email, get_user_email_field_name
from djoser.conf import settings

User = get_user_model()


class RootView(views.APIView):
    """
    Root endpoint - use one of sub endpoints.
    """
    permission_classes = [permissions.AllowAny]

    def aggregate_djoser_urlpattern_names(self):
        from djoser.urls import base, authtoken
        urlpattern_names = [pattern.name for pattern in base.urlpatterns]
        urlpattern_names += [pattern.name for pattern in authtoken.urlpatterns]
        urlpattern_names += self._get_jwt_urlpatterns()

        return urlpattern_names

    def get_urls_map(self, request, urlpattern_names, fmt):
        urls_map = {}
        for urlpattern_name in urlpattern_names:
            try:
                url = reverse(urlpattern_name, request=request, format=fmt)
            except NoReverseMatch:
                url = ''
            urls_map[urlpattern_name] = url
        return urls_map

    def get(self, request, fmt=None):
        urlpattern_names = self.aggregate_djoser_urlpattern_names()
        urls_map = self.get_urls_map(request, urlpattern_names, fmt)
        return Response(urls_map)

    def _get_jwt_urlpatterns(self):
        try:
            from djoser.urls import jwt
            return [pattern.name for pattern in jwt.urlpatterns]
        except ImportError:
            return []


class UserCreateView(generics.CreateAPIView):
    """
    Use this endpoint to register new user.
    """
    serializer_class = settings.SERIALIZERS.user_create
    permission_classes = [permissions.AllowAny]
    _users = None

    def create(self, request, *args, **kwargs):
        users = self.get_users()
        for user in users:
            serializer = self.get_serializer(instance=user)
            if settings.RESEND_REGISTRATION_EMAIL:
                self.resend_registration_email(user)
            headers = self.get_success_headers(serializer.data)
        if not settings.REGISTRATION_SHOW_EMAIL_FOUND and users:
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
        return super(UserCreateView, self).create(request, *args, **kwargs)

    def perform_create(self, serializer):
        user = serializer.save()
        signals.user_registered.send(
            sender=self.__class__, user=user, request=self.request
        )

        context = {'user': user}
        to = [get_user_email(user)]
        if settings.SEND_ACTIVATION_EMAIL:
            settings.EMAIL.activation(self.request, context).send(to)
        elif settings.SEND_CONFIRMATION_EMAIL:
            settings.EMAIL.confirmation(self.request, context).send(to)

    def get_users(self):
        if self._users is None:
            email_field_name = get_user_email_field_name(User)
            email = self.request.data.get(email_field_name)
            username = self.request.data.get(User.USERNAME_FIELD)
            email_users_kwargs = {
                email_field_name + '__iexact': email,
            }
            username_users_kwargs = {
                User.USERNAME_FIELD + '__iexact': username,
            }
            email_users_filter = Q(**email_users_kwargs)
            username_users_filter = Q(**username_users_kwargs)
            email_users = User._default_manager.filter(
                username_users_filter | email_users_filter)
            self._users = [u for u in email_users if u.has_usable_password()]
        return self._users

    def get_email_users(self, email):
        if self._users is None:
            email_field_name = get_user_email_field_name(User)
            email_users_kwargs = {
                email_field_name + '__iexact': email,
            }
            email_users = User._default_manager.filter(**email_users_kwargs)
            self._users = [u for u in email_users if u.has_usable_password()]
        return self._users

    def resend_registration_email(self, user):
        context = {'user': user}
        to = [get_user_email(user)]
        if user.is_active:
            settings.EMAIL.password_reset(self.request, context).send(to)
        else:
            settings.EMAIL.activation(self.request, context).send(to)


class UserDeleteView(generics.CreateAPIView):
    """
    Use this endpoint to remove actually authenticated user
    """
    serializer_class = settings.SERIALIZERS.user_delete
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def post(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)

        utils.logout_user(self.request)
        instance.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class TokenCreateView(utils.ActionViewMixin, generics.GenericAPIView):
    """
    Use this endpoint to obtain user authentication token.
    """
    serializer_class = settings.SERIALIZERS.token_create
    permission_classes = [permissions.AllowAny]

    def _action(self, serializer):
        token = utils.login_user(self.request, serializer.user)
        token_serializer_class = settings.SERIALIZERS.token
        return Response(
            data=token_serializer_class(token).data,
            status=status.HTTP_200_OK,
        )


class TokenDestroyView(views.APIView):
    """
    Use this endpoint to logout user (remove user authentication token).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        utils.logout_user(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetView(utils.ActionViewMixin, generics.GenericAPIView):
    """
    Use this endpoint to send email to user with password reset link.
    """
    serializer_class = settings.SERIALIZERS.password_reset
    permission_classes = [permissions.AllowAny]

    _users = None

    def _action(self, serializer):
        for user in self.get_users(serializer.data['email']):
            self.send_password_reset_email(user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get_users(self, email):
        if self._users is None:
            email_field_name = get_user_email_field_name(User)
            users = User._default_manager.filter(**{
                email_field_name + '__iexact': email
            })
            self._users = [
                u for u in users if u.is_active and u.has_usable_password()
            ]
        return self._users

    def send_password_reset_email(self, user):
        context = {'user': user}
        to = [get_user_email(user)]
        settings.EMAIL.password_reset(self.request, context).send(to)


class SetPasswordView(utils.ActionViewMixin, generics.GenericAPIView):
    """
    Use this endpoint to change user password.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if settings.SET_PASSWORD_RETYPE:
            return settings.SERIALIZERS.set_password_retype
        return settings.SERIALIZERS.set_password

    def _action(self, serializer):
        self.request.user.set_password(serializer.data['new_password'])
        self.request.user.save()

        if settings.LOGOUT_ON_PASSWORD_CHANGE:
            utils.logout_user(self.request)

        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetConfirmView(utils.ActionViewMixin, generics.GenericAPIView):
    """
    Use this endpoint to finish reset password process.
    """
    permission_classes = [permissions.AllowAny]
    token_generator = default_token_generator

    def get_serializer_class(self):
        if settings.PASSWORD_RESET_CONFIRM_RETYPE:
            return settings.SERIALIZERS.password_reset_confirm_retype
        return settings.SERIALIZERS.password_reset_confirm

    def _action(self, serializer):
        serializer.user.set_password(serializer.data['new_password'])
        serializer.user.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ActivationView(utils.ActionViewMixin, generics.GenericAPIView):
    """
    Use this endpoint to activate user account.
    """
    serializer_class = settings.SERIALIZERS.activation
    permission_classes = [permissions.AllowAny]
    token_generator = default_token_generator

    def _action(self, serializer):
        user = serializer.user
        user.is_active = True
        user.save()

        signals.user_activated.send(
            sender=self.__class__, user=user, request=self.request
        )

        if settings.SEND_CONFIRMATION_EMAIL:
            context = {'user': user}
            to = [get_user_email(user)]
            settings.EMAIL.confirmation(self.request, context).send(to)

        return Response(status=status.HTTP_204_NO_CONTENT)


class SetUsernameView(utils.ActionViewMixin, generics.GenericAPIView):
    """
    Use this endpoint to change user username.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if settings.SET_USERNAME_RETYPE:
            return settings.SERIALIZERS.set_username_retype
        return settings.SERIALIZERS.set_username

    def _action(self, serializer):
        user = self.request.user
        new_username = serializer.data['new_' + User.USERNAME_FIELD]

        setattr(user, User.USERNAME_FIELD, new_username)
        if settings.SEND_ACTIVATION_EMAIL:
            user.is_active = False
            context = {'user': user}
            to = [get_user_email(user)]
            settings.EMAIL.activation(self.request, context).send(to)
        user.save()

        return Response(status=status.HTTP_204_NO_CONTENT)


class UserView(generics.RetrieveUpdateAPIView):
    """
    Use this endpoint to retrieve/update user.
    """
    model = User
    serializer_class = settings.SERIALIZERS.user
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self, *args, **kwargs):
        return self.request.user

    def perform_update(self, serializer):
        super(UserView, self).perform_update(serializer)
        user = serializer.instance
        if settings.SEND_ACTIVATION_EMAIL and not user.is_active:
            context = {'user': user}
            to = [get_user_email(user)]
            settings.EMAIL.activation(self.request, context).send(to)
