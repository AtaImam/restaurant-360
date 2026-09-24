from django import forms
from .models import Expense, ExpenseCategory


class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "description", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Utilities, Rent, Packaging"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Optional description..."}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = [
            "category",
            "title",
            "amount",
            "expense_date",
            "payment_method",
            "payee",
            "receipt_image",
            "notes",
        ]
        widgets = {
            "category": forms.Select(attrs={"class": "form-control"}),
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Electric bill for September"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "0.00"}),
            "expense_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "payment_method": forms.Select(attrs={"class": "form-control"}),
            "payee": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. City Power Corp"}),
            "receipt_image": forms.FileInput(attrs={"class": "form-control", "accept": "image/*"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Additional notes..."}),
        }

    def __init__(self, *args, restaurant=None, **kwargs):
        super().__init__(*args, **kwargs)
        if restaurant:
            self.fields["category"].queryset = ExpenseCategory.objects.filter(
                restaurant=restaurant,
                is_active=True,
            )
        else:
            self.fields["category"].queryset = ExpenseCategory.objects.filter(is_active=True)
