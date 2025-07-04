import hmac
import json
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from notification.models import Notification
from user.models import User, UserVerification
from utils.siftscience import events_api, update_user_risk_score

logger = logging.getLogger(__name__)


class PersonaWebhookView(APIView):
    """
    View for processing Persona webhooks.

    This view handles incoming POST requests from Persona.
    """

    permission_classes = [AllowAny]

    def post(self, request: Request, *args, **kwargs) -> Response:
        """
        Process incoming webhook from Persona.
        """
        try:
            persona_signature = request.headers.get("Persona-Signature")

            if not persona_signature:
                return Response(
                    {"message": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED
                )

            if not self._validate_signature(request):
                return Response(
                    {"message": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED
                )

            self._process_payload(request)
        except Exception as e:
            logger.error(f"Failed to process webhook payload: {e}")
            return Response(
                {"message": "Failed to process webhook"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"message": "Webhook successfully processed"}, status=status.HTTP_200_OK
        )

    def _get_nested_attr(self, data, keys, default=None):
        if isinstance(keys, str):
            keys = keys.split(".")

        for key in keys:
            try:
                data = data[key]
            except (TypeError, KeyError):
                return default
        return data

    def _process_payload(self, request: Request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON payload")

        persona_status = self._get_nested_attr(
            data, "data.attributes.payload.data.attributes.status"
        ).lower()
        status = None
        if persona_status == "approved":
            status = UserVerification.Status.APPROVED
        elif persona_status == "declined":
            status = UserVerification.Status.DECLINED
        elif persona_status == "failed":
            status = UserVerification.Status.FAILED
        elif persona_status == "marked-for-review":
            status = UserVerification.Status.MARKED_FOR_REVIEW
        else:
            status = UserVerification.Status.PENDING

        reference_id = self._get_nested_attr(
            data, "data.attributes.payload.data.attributes.reference-id"
        )
        first_name = self._get_nested_attr(
            data, "data.attributes.payload.data.attributes.name-first"
        )
        last_name = self._get_nested_attr(
            data, "data.attributes.payload.data.attributes.name-last"
        )
        inquiry_id = self._get_nested_attr(data, "data.attributes.payload.data.id")

        user_verification, _ = UserVerification.objects.update_or_create(
            user_id=reference_id,
            defaults={
                "first_name": first_name,
                "last_name": last_name,
                "verified_by": UserVerification.Type.PERSONA,
                "external_id": inquiry_id,
                "status": status,
            },
        )
        user_verification.save()

        self._create_notification(user_verification)

        user = User.objects.get(id=user_verification.user_id)
        tracked_account = events_api.track_account(user, request, update=True)
        update_user_risk_score(user, tracked_account)

    def _create_notification(self, user_verification: UserVerification):
        user = User.objects.get(id=user_verification.user_id)
        notification = Notification.objects.create(
            action_user=user,
            extra={
                "status": user_verification.status.value,
            },
            item=user_verification,
            notification_type=Notification.IDENTITY_VERIFICATION_UPDATED,
            recipient=user,
        )
        notification.send_notification()

    def _validate_signature(self, request: Request) -> bool:
        """
        Validate the signature of the incoming request.

        Also see: https://docs.withpersona.com/docs/webhooks-best-practices#checking-signatures
        """
        t, v1 = [
            value.split("=")[1]
            for value in request.headers["Persona-Signature"].split(",")
        ]

        computed_digest = self.create_digest(
            settings.PERSONA_WEBHOOK_SECRET, t, request.body.decode("utf-8")
        )

        return hmac.compare_digest(v1, computed_digest)

    @classmethod
    def create_digest(cls, key: str, t: str, body: str) -> str:
        return hmac.new(
            key.encode(),
            (t + "." + body).encode(),
            "sha256",
        ).hexdigest()
