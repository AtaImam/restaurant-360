from copy import deepcopy
from django import forms
from .schema import FIELDS, json_value


class SettingsForm(forms.Form):
    def __init__(self, *args, section, effective, overrides=None, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.section = section
        self.is_override = overrides is not None
        for name, field in FIELDS[section].items():
            self.fields[name] = deepcopy(field)
            self.fields[name].initial = effective[name]
            if self.is_override:
                self.fields['override_' + name] = forms.BooleanField(required=False, initial=name in overrides, label='Use branch override', widget=forms.CheckboxInput(attrs={'data-setting': 'id_' + name}))
                # Unchecked fields are inherited, and need not contain valid input.
                if self.is_bound and not self.data.get('override_' + name):
                    self.fields[name].disabled = True
        if section == 'business' and business is not None:
            for name in ('name', 'address', 'phone'):
                self.fields['business_' + name] = forms.CharField(required=name == 'name', max_length=150 if name == 'name' else 1000 if name == 'address' else 20,
                    initial=getattr(business, name), label='Business ' + name)
        self.rows = [(self[name], self['override_' + name] if self.is_override else None) for name in FIELDS[section]]

    def updated_values(self, existing):
        values = dict(existing)
        for name in FIELDS[self.section]:
            if self.is_override and not self.cleaned_data['override_' + name]:
                values.pop(name, None)
            else:
                values[name] = json_value(self.cleaned_data[name])
        return values
