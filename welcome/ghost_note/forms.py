from datetime import timedelta

from django import forms
from django.utils import timezone

from .models import GhostPurchaseOrder
from .telegram_notify import normalize_telegram_input, TELEGRAM_USERNAME_RE
from .yookassa_client import MAX_DURATION_MINUTES, PRICE_LOCAL, PRICE_REMOTE


class PurchaseForm(forms.Form):
    DURATION_CHOICES = [
        (30, '30 минут'),
        (60, '1 час'),
        (90, '1,5 часа'),
        (120, '2 часа'),
        (150, '2,5 часа'),
        (180, '3 часа'),
    ]

    customer_name = forms.CharField(
        label='Ваше имя',
        max_length=128,
        widget=forms.TextInput(attrs={
            'placeholder': 'Как к вам обращаться',
            'autocomplete': 'name',
        }),
    )
    customer_telegram = forms.CharField(
        label='Telegram для получения токена',
        max_length=64,
        widget=forms.TextInput(attrs={
            'placeholder': '123456789 или @username',
            'autocomplete': 'off',
        }),
        help_text='Сначала напишите боту /start — иначе доставка по @username не сработает.',
    )
    customer_email = forms.EmailField(
        label='E-mail для получения токена',
        widget=forms.EmailInput(attrs={
            'placeholder': 'example@mail.ru',
            'autocomplete': 'email',
        }),
        help_text='На этот адрес придёт токен и инструкция после оплаты.',
    )
    referral_key = forms.CharField(
        label='Реферальный ключ пригласившего',
        max_length=16,
        required=False,
        widget=forms.TextInput(attrs={
            'placeholder': 'Необязательно, 8 символов',
            'autocomplete': 'off',
            'style': 'text-transform: uppercase',
        }),
    )
    access_type = forms.ChoiceField(
        label='Тип доступа',
        choices=GhostPurchaseOrder.AccessType.choices,
        widget=forms.RadioSelect,
        initial=GhostPurchaseOrder.AccessType.LOCAL,
    )
    duration_minutes = forms.ChoiceField(
        label='Длительность доступа',
        choices=DURATION_CHOICES,
        initial=120,
    )
    starts_at = forms.DateTimeField(
        label='Начало доступа',
        input_formats=['%Y-%m-%dT%H:%M'],
        widget=forms.DateTimeInput(
            attrs={'type': 'datetime-local'},
            format='%Y-%m-%dT%H:%M',
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.initial.get('starts_at') and not self.data:
            now = timezone.localtime()
            rounded = now.replace(second=0, microsecond=0)
            self.initial['starts_at'] = rounded

    def clean_referral_key(self):
        return (self.cleaned_data.get('referral_key') or '').strip().upper()

    def clean_customer_telegram(self):
        value = normalize_telegram_input(self.cleaned_data.get('customer_telegram'))
        if not value:
            raise forms.ValidationError('Укажите Telegram ID или @username.')
        if value.lstrip('-').isdigit():
            return value
        if TELEGRAM_USERNAME_RE.match(value):
            return f'@{value}'
        raise forms.ValidationError(
            'Укажите числовой Telegram ID или @username (5–32 символа, латиница).'
        )

    def clean_duration_minutes(self):
        value = int(self.cleaned_data['duration_minutes'])
        if value < 1 or value > MAX_DURATION_MINUTES:
            raise forms.ValidationError(f'Длительность — не более {MAX_DURATION_MINUTES} минут.')
        return value

    def clean_starts_at(self):
        starts_at = self.cleaned_data['starts_at']
        if timezone.is_naive(starts_at):
            starts_at = timezone.make_aware(starts_at, timezone.get_current_timezone())
        now = timezone.now()
        if starts_at < now - timedelta(minutes=5):
            raise forms.ValidationError('Время начала не может быть в прошлом.')
        if starts_at > now + timedelta(days=7):
            raise forms.ValidationError('Время начала — не позже чем через 7 дней.')
        return starts_at

    def clean(self):
        cleaned = super().clean()
        access_type = cleaned.get('access_type')
        duration = cleaned.get('duration_minutes')
        starts_at = cleaned.get('starts_at')
        if access_type and duration and starts_at:
            ends_at = starts_at + timedelta(minutes=duration)
            if ends_at <= starts_at:
                raise forms.ValidationError('Проверьте длительность и время начала.')
        return cleaned

    def price_label(self):
        access_type = self.data.get('access_type') or self.initial.get('access_type', 'local')
        if access_type == GhostPurchaseOrder.AccessType.REMOTE:
            return f'{PRICE_REMOTE:,.0f}'.replace(',', ' ')
        return f'{PRICE_LOCAL:,.0f}'.replace(',', ' ')
