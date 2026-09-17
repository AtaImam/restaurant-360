from django import forms
from django.core.exceptions import ValidationError

from .models import Floor, Table


class FloorForm(forms.ModelForm):
    class Meta:
        model = Floor
        fields = [
            "name",
            "floor_number",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Example: Ground Floor",
                }
            ),
            "floor_number": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Example: 0",
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
        }

    def __init__(self, *args, restaurant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.restaurant = restaurant

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        if not name:
            raise ValidationError("Floor name is required.")

        duplicate_floors = Floor.objects.filter(
            restaurant=self.restaurant,
            name__iexact=name,
        )

        if self.instance.pk:
            duplicate_floors = duplicate_floors.exclude(
                pk=self.instance.pk
            )

        if duplicate_floors.exists():
            raise ValidationError(
                "A floor with this name already exists."
            )

        return name

    def clean_floor_number(self):
        floor_number = self.cleaned_data["floor_number"]

        duplicate_floors = Floor.objects.filter(
            restaurant=self.restaurant,
            floor_number=floor_number,
        )

        if self.instance.pk:
            duplicate_floors = duplicate_floors.exclude(
                pk=self.instance.pk
            )

        if duplicate_floors.exists():
            raise ValidationError(
                "A floor with this number already exists."
            )

        return floor_number

    def save(self, commit=True):
        floor = super().save(commit=False)
        floor.restaurant = self.restaurant

        if commit:
            floor.save()

        return floor


class TableForm(forms.ModelForm):
    class Meta:
        model = Table
        fields = [
            "floor",
            "table_number",
            "capacity",
            "status",
            "is_active",
        ]
        widgets = {
            "floor": forms.Select(
                attrs={"class": "form-control"}
            ),
            "table_number": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": 1,
                    "placeholder": "Example: 11",
                }
            ),
            "capacity": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": 1,
                    "max": 50,
                    "placeholder": "Example: 4",
                }
            ),
            "status": forms.Select(
                attrs={"class": "form-control"}
            ),
            "is_active": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
        }

    def __init__(self, *args, restaurant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.restaurant = restaurant

        self.fields["floor"].queryset = Floor.objects.filter(
            restaurant=restaurant,
            is_active=True,
        ).order_by("floor_number", "name")

        self.fields["floor"].empty_label = "Select floor"

    def clean_floor(self):
        floor = self.cleaned_data["floor"]

        if floor.restaurant_id != self.restaurant.id:
            raise ValidationError(
                "The selected floor does not belong to this restaurant."
            )

        return floor

    def clean_table_number(self):
        table_number = self.cleaned_data["table_number"]

        duplicate_tables = Table.objects.filter(
            restaurant=self.restaurant,
            table_number=table_number,
        )

        if self.instance.pk:
            duplicate_tables = duplicate_tables.exclude(
                pk=self.instance.pk
            )

        if duplicate_tables.exists():
            raise ValidationError(
                "This table number already exists."
            )

        return table_number

    def clean_capacity(self):
        capacity = self.cleaned_data["capacity"]

        if capacity < 1:
            raise ValidationError(
                "Capacity must be at least 1."
            )

        if capacity > 50:
            raise ValidationError(
                "Capacity cannot be greater than 50."
            )

        return capacity

    def save(self, commit=True):
        table = super().save(commit=False)
        table.restaurant = self.restaurant

        if commit:
            table.save()

        return table