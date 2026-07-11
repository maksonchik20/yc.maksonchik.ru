from django.core.management.base import BaseCommand

from ghost_note.models import GhostAccessToken, GhostReferralCommission


class Command(BaseCommand):
    help = 'Recalculate referral commissions for all real tokens.'

    def handle(self, *args, **options):
        synced = 0
        for token in GhostAccessToken.objects.select_related('user', 'user__referred_by').order_by('id'):
            before = GhostReferralCommission.objects.filter(token=token).count()
            token.sync_referral_commission()
            after = GhostReferralCommission.objects.filter(token=token).count()
            if after or before:
                synced += 1
                commission = GhostReferralCommission.objects.filter(token=token).first()
                if commission:
                    self.stdout.write(
                        f'{token.token} | {token.label} | '
                        f'{commission.referrer.name} ← {commission.referred_user.name} | '
                        f'{commission.commission_amount} ₽'
                    )
                elif before:
                    self.stdout.write(f'{token.token} | {token.label} | commission removed')

        total = GhostReferralCommission.objects.count()
        self.stdout.write(self.style.SUCCESS(f'Synced {synced} tokens, {total} commissions total.'))
