import json
import time
import unicodedata
from uuid import uuid4

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .services import (
    ProductPromptService,
    PromptCommandInterpreter,
    PromptCommandProcessor,
    PromptActionCancelled,
    PromptPendingAction,
    PromptServiceError,
)


PENDING_SESSION_KEY = "prompt_pending_commands"
BOOLEAN_TRUE = {"true", "1", "yes", "y", "si", "s", "ok", "vale", "claro", "confirmo"}
BOOLEAN_FALSE = {"false", "0", "no", "n", "cancel", "cancelar", "rechazo", "salir"}


@require_POST
def submit_prompt(request):
    """Receive a prompt message, forward it to the LLM, and return the answer."""

    try:
        payload = json.loads(request.body.decode('utf-8'))
        print("Payload:", payload)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON payload.'}, status=400)

    pending_token = (payload.get('pending_token') or '').strip()
    command_processor = PromptCommandProcessor(PromptCommandInterpreter())

    if pending_token:
        return _resume_pending_command(request, payload, pending_token, command_processor)

    message = (payload.get('message') or '').strip()
    if not message:
        return JsonResponse({'error': 'El mensaje no puede estar vacio.'}, status=400)

    auto_pending_response = _auto_resume_pending(request, message, command_processor)
    if auto_pending_response is not None:
        return auto_pending_response

    try:
        command_result = command_processor.process_if_command(message)
    except PromptPendingAction as pending_exc:
        return _store_pending(request, pending_exc)
    except PromptActionCancelled as exc:
        return JsonResponse({'status': 'cancelled', 'detail': str(exc), 'actions': []})
    except PromptServiceError as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    if command_result is not None:
        return JsonResponse(
            {
                'status': 'ok',
                'detail': command_result.get('detail') or 'Comando ejecutado correctamente.',
                'answer': command_result.get('answer', ''),
                'actions': command_result.get('actions', []),
            }
        )

    service = ProductPromptService()
    try:
        answer = service.answer_question(message)
    except PromptServiceError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    except Exception:
        return JsonResponse({'error': 'Error inesperado al procesar el mensaje.'}, status=500)

    return JsonResponse(
        {
            'status': 'ok',
            'detail': 'Mensaje recibido correctamente.',
            'answer': answer,
            'actions': [],
        }
    )


def _resume_pending_command(request, payload, token, command_processor):
    store = _get_pending_store(request)
    record = store.get(token)
    if not record:
        return JsonResponse({'error': 'La solicitud pendiente expiro o es invalida.'}, status=404)

    base_command = record.get('command') or {}
    action = base_command.get('action')
    if not action:
        store.pop(token, None)
        request.session.modified = True
        return JsonResponse({'error': 'El comando pendiente es invalido.'}, status=500)

    merged_data = dict(base_command.get('data') or {})

    extra_data = payload.get('data')
    if isinstance(extra_data, dict):
        merged_data.update(extra_data)

    requirements = record.get('requirements') or []
    message_text = (payload.get('message') or '').strip()

    if requirements and message_text and not isinstance(extra_data, dict):
        field_name = requirements[0].get('field')
        if field_name:
            merged_data[field_name] = message_text

    if record.get('requires_confirmation'):
        confirmed, recognized = _normalize_bool(payload.get('confirm'))
        if not recognized:
            confirmed, recognized = _normalize_bool(payload.get('confirmation'))
        if not recognized and message_text:
            confirmed, recognized = _normalize_bool(message_text)
        if recognized:
            if not confirmed:
                store.pop(token, None)
                request.session.modified = True
                return JsonResponse(
                    {'status': 'cancelled', 'detail': 'La operacion fue cancelada por el usuario.', 'actions': []}
                )
            merged_data['confirm'] = True

    payload_for_processor = json.dumps({
        'commands': [
            {
                'action': action,
                'data': merged_data,
            }
        ]
    })

    try:
        result = command_processor.process_if_command(payload_for_processor)
    except PromptPendingAction as pending_exc:
        return _store_pending(request, pending_exc, token=token)
    except PromptActionCancelled as exc:
        store.pop(token, None)
        request.session.modified = True
        return JsonResponse({'status': 'cancelled', 'detail': str(exc), 'actions': []})
    except PromptServiceError as exc:
        store.pop(token, None)
        request.session.modified = True
        return JsonResponse({'error': str(exc)}, status=400)

    store.pop(token, None)
    request.session.modified = True

    if result is not None:
        return JsonResponse(
            {
                'status': 'ok',
                'detail': result.get('detail') or 'Comando ejecutado correctamente.',
                'answer': result.get('answer', ''),
                'actions': result.get('actions', []),
            }
        )

    return JsonResponse(
        {
            'status': 'ok',
            'detail': 'Comando ejecutado correctamente.',
            'answer': '',
            'actions': [],
        }
    )


def _store_pending(request, pending_exc, *, token: str | None = None):
    store = _get_pending_store(request)
    if token is None:
        token = uuid4().hex

    command_payload = pending_exc.pending_command or {}
    safe_command = {
        'action': command_payload.get('action'),
        'data': dict(command_payload.get('data') or {}),
    }

    existing_record = store.get(token) if isinstance(store, dict) else None
    created_at = existing_record.get('created_at') if isinstance(existing_record, dict) else None
    if created_at is None:
        created_at = time.time()

    store[token] = {
        'command': safe_command,
        'requirements': list(pending_exc.requirements or []),
        'requires_confirmation': bool(pending_exc.confirmation_message),
        'confirmation_message': pending_exc.confirmation_message or '',
        'created_at': created_at,
    }
    request.session[PENDING_SESSION_KEY] = store
    request.session.modified = True

    response_payload = {
        'status': 'pending',
        'detail': str(pending_exc),
        'pending_token': token,
    }
    if pending_exc.requirements:
        response_payload['requirements'] = pending_exc.requirements
    if pending_exc.confirmation_message:
        response_payload['confirmation_message'] = pending_exc.confirmation_message

    response_payload['answer'] = _build_pending_answer(token, pending_exc)
    actions = _build_pending_actions(token, pending_exc)
    response_payload['actions'] = actions

    return JsonResponse(response_payload)


def _get_pending_store(request):
    store = request.session.get(PENDING_SESSION_KEY)
    if not isinstance(store, dict):
        store = {}
    request.session[PENDING_SESSION_KEY] = store
    return store


def _normalize_bool(value):
    if isinstance(value, bool):
        return value, True
    if value is None:
        return None, False
    if isinstance(value, (int, float)):
        return bool(value), True
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    text = ascii_text.strip().lower()
    if not text:
        return None, False
    if text in BOOLEAN_TRUE:
        return True, True
    if text in BOOLEAN_FALSE:
        return False, True
    return None, False


def _auto_resume_pending(request, message, command_processor):
    if not message or message.startswith('{'):
        return None

    store = request.session.get(PENDING_SESSION_KEY)
    if not isinstance(store, dict) or not store:
        return None

    candidates = [
        (pending_token, record)
        for pending_token, record in store.items()
        if isinstance(record, dict)
    ]
    if not candidates:
        return None

    token, record = max(candidates, key=lambda item: item[1].get('created_at', 0))
    confirmation_required = bool(record.get('requires_confirmation'))
    requirements = record.get('requirements') or []

    if confirmation_required:
        _, recognized = _normalize_bool(message)
        if not recognized:
            return None

    if not confirmation_required and not requirements:
        return None

    synthetic_payload = {'message': message}
    return _resume_pending_command(request, synthetic_payload, token, command_processor)


def _build_pending_answer(token, pending_exc):
    pieces = []
    if pending_exc.confirmation_message:
        pieces.append(pending_exc.confirmation_message)
        pieces.append(
            "Puedes confirmar respondiendo 'si' o enviando {\"pending_token\": \"%s\", \"confirm\": true}."
            % token
        )

    if pending_exc.requirements:
        prompts = []
        for requirement in pending_exc.requirements:
            label = requirement.get('label') or requirement.get('field') or 'Dato requerido'
            prompt = requirement.get('prompt') or 'Proporciona el valor necesario.'
            prompts.append(f"{label}: {prompt}")
        if prompts:
            pieces.append("Datos pendientes: " + " ".join(prompts))
            pieces.append(
                "Envía el valor contestando con texto o usando {\"pending_token\": \"%s\", \"data\": {...}}."
                % token
            )

    if not pieces:
        pieces.append(
            "Responde con el token pendiente {\"pending_token\": \"%s\"} para completar la operacion." % token
        )
    return " ".join(pieces)


def _build_pending_actions(token, pending_exc):
    actions = []
    if pending_exc.confirmation_message:
        actions.append(
            {
                'label': 'Confirmar',
                'type': 'submit',
                'payload': {'pending_token': token, 'confirm': True},
            }
        )
        actions.append(
            {
                'label': 'Cancelar',
                'type': 'submit',
                'payload': {'pending_token': token, 'confirm': False},
            }
        )

    if pending_exc.requirements:
        for requirement in pending_exc.requirements:
            field_name = requirement.get('field') or 'value'
            label = requirement.get('label') or field_name
            actions.append(
                {
                    'label': f"Proporcionar {label}",
                    'type': 'input',
                    'payload': {'pending_token': token, 'field': field_name},
                }
            )

    return actions
