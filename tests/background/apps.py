from django.apps import AppConfig

from django_logic import ProcessManager


class BackgroundTestsConfig(AppConfig):
    name = 'tests.background'
    label = 'bg_tests'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        # The single binding site for this app. ready() runs after every app's
        # models are imported (Django app-loading phase 3), so binding here can
        # never trigger the model→process→actions→model import cycle (issue
        # #100). Keep model/process imports inside ready(), not at module top.
        from .models import (
            AmbiguousConversationProcess,
            ArchivableProcess,
            ArchivableWidget,
            Conversation,
            ConversationProcess,
            MixedSyncBgProcess,
            ScenarioGuardProcess,
            SharedActionConversationProcess,
            Widget,
            WidgetAuditProcess,
            WidgetChainProcess,
            WidgetParentProcess,
            WidgetProcess,
        )

        ProcessManager.bind_model_process(Widget, WidgetProcess, state_field='status')
        ProcessManager.bind_model_process(Widget, WidgetAuditProcess, state_field='audit_status')
        ProcessManager.bind_model_process(Widget, WidgetParentProcess, state_field='status')
        ProcessManager.bind_model_process(ArchivableWidget, ArchivableProcess, state_field='status')
        ProcessManager.bind_model_process(Conversation, ConversationProcess, state_field='status')
        ProcessManager.bind_model_process(Conversation, AmbiguousConversationProcess, state_field='status')
        ProcessManager.bind_model_process(Conversation, SharedActionConversationProcess, state_field='status')
        ProcessManager.bind_model_process(Conversation, MixedSyncBgProcess, state_field='status')
        # Test-local processes attached to Widget (see models.py).
        ProcessManager.bind_model_process(Widget, ScenarioGuardProcess, state_field='status')
        ProcessManager.bind_model_process(Widget, WidgetChainProcess, state_field='status')
