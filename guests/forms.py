import re
from datetime import datetime

from django import forms
from django.utils import timezone

from .models import Reservation


class ReservationForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = ['name', 'phone', 'date', 'time', 'party_size', 'note']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'time': forms.TimeInput(attrs={'type': 'time'}),
            'phone': forms.TextInput(attrs={'type': 'tel'}),
            'note': forms.Textarea(attrs={'rows': 3}),
        }

    def clean_phone(self):
        phone = self.cleaned_data['phone']
        if not re.fullmatch(r'\+?[0-9 ()-]+', phone) or not 7 <= len(re.sub(r'\D', '', phone)) <= 15:
            raise forms.ValidationError('Enter a valid phone number.')
        return phone

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('date') and cleaned.get('time'):
            when = timezone.make_aware(datetime.combine(cleaned['date'], cleaned['time']))
            if when <= timezone.now():
                raise forms.ValidationError('Choose a future date and time.')
        return cleaned


class FeedbackForm(forms.Form):
    rating = forms.TypedChoiceField(choices=[(i, str(i)) for i in range(1, 6)], coerce=int, widget=forms.RadioSelect)
    comment = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={'rows': 3}))
