# Standard Library
from unittest.mock import Mock, patch

# Django
from django import forms
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase

# AA BigBrother
from aa_bb.admin import TicketToolConfigAdmin
from aa_bb.models import TicketToolConfig


class TestTicketToolConfigAdmin(TestCase):
    def setUp(self):
        self.model_admin = TicketToolConfigAdmin(TicketToolConfig, AdminSite())
        self.request = RequestFactory().get("/admin/aa_bb/tickettoolconfig/")

    @patch("aa_bb.admin.charlink_active", return_value=True)
    def test_fieldsets_use_compliance_filter_id(self, _mock_charlink_active):
        fieldsets = self.model_admin.get_fieldsets(self.request)
        fields = []
        for _title, options in fieldsets:
            fields.extend(options.get("fields", ()))

        self.assertIn("compliance_filter_id", fields)
        self.assertNotIn("compliance_filter", fields)

    @patch("aa_bb.admin.charlink_active", return_value=True)
    @patch("aa_bb.admin.apps.get_model")
    def test_get_form_exposes_model_choice_for_compliance_filter_id(self, mock_get_model, _mock_charlink_active):
        queryset = TicketToolConfig.objects.none()
        filter_result = Mock()
        filter_result.first.return_value = None
        manager = Mock()
        manager.all.return_value = queryset
        manager.filter.return_value = filter_result

        compliance_filter_model = Mock()
        compliance_filter_model.objects = manager
        mock_get_model.return_value = compliance_filter_model

        form_class = self.model_admin.get_form(self.request)
        compliance_filter_field = form_class.base_fields["compliance_filter_id"]

        self.assertIsInstance(compliance_filter_field, forms.ModelChoiceField)
        self.assertEqual(compliance_filter_field.queryset, queryset)
