from django.contrib import admin
from django.contrib.auth.models import User, Group
from django.contrib.auth.admin import UserAdmin, GroupAdmin
from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.utils.html import format_html
from django.urls import path, reverse
from django.shortcuts import render, redirect
from django.db.models import Sum, Count
from django.db.models.functions import TruncMonth, TruncDate
from django.utils import timezone
from django.http import JsonResponse, HttpResponse
from django.core.serializers.json import DjangoJSONEncoder
from datetime import timedelta
from django import forms
import json
import csv
from .models import Car, CarColor, CarImage, Customer, Payment, Sell, Inquiry, EMIPlan, CarReview, FinanceSetting, ContactMessage, UserProfile, TestDrive, Notification, NotificationRead, ContactInfo


def export_as_csv(modeladmin, request, queryset):
    """
    Generic CSV export action for any model
    """
    meta = modeladmin.model._meta
    field_names = [field.name for field in meta.fields]
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename={meta.verbose_name_plural.replace(" ", "_")}.csv'
    
    writer = csv.writer(response)
    writer.writerow(field_names)
    
    for obj in queryset:
        row = []
        for field in field_names:
            value = getattr(obj, field)
            if callable(value):
                value = value()
            row.append(str(value) if value is not None else '')
        writer.writerow(row)
    
    return response

export_as_csv.short_description = "Export Selected to CSV"


class CarStoreAdminSite(admin.AdminSite):
    site_header = '🚗 Car Store Admin'
    site_title = 'Car Store Admin Portal'
    index_title = 'Welcome to Car Store Administration'
    
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('dashboard/', self.admin_view(self.dashboard_view), name='dashboard'),
            path('api/chart-data/', self.admin_view(self.chart_data_api), name='chart_data'),
        ]
        return custom_urls + urls
    
    def chart_data_api(self, request):
        """API endpoint for real-time chart data"""
        today = timezone.now().date()
        this_month_start = today.replace(day=1)
        
        # Basic stats
        total_cars = Car.objects.count()
        available_cars = Car.objects.filter(is_available=True).count()
        sold_cars = total_cars - available_cars
        total_sells = Sell.objects.count()
        total_customers = Customer.objects.count()
        total_revenue = Payment.objects.filter(payment_status='completed').aggregate(Sum('amount'))['amount__sum'] or 0
        pending_payments = Payment.objects.filter(payment_status='pending').count()
        total_inquiries = Inquiry.objects.count()
        unresolved_inquiries = Inquiry.objects.filter(is_resolved=False).count()
        resolved_inquiries = Inquiry.objects.filter(is_resolved=True).count()
        monthly_sells = Sell.objects.filter(sell_date__gte=this_month_start).count()
        
        # Conversion rate
        conversion_rate = round((total_sells / resolved_inquiries * 100) if resolved_inquiries > 0 else 0, 1)
        
        # EMI Stats
        active_emi = EMIPlan.objects.filter(plan_status='active').count()
        overdue_emi = EMIPlan.objects.filter(plan_status='active', next_due_date__lt=today).count()
        emi_outstanding = float(EMIPlan.objects.filter(plan_status='active').aggregate(total=Sum('loan_amount'))['total'] or 0)
        
        # Cars by fuel type
        cars_by_fuel = list(Car.objects.values('fuel_type').annotate(count=Count('id')))
        
        # Cars by brand
        cars_by_brand = list(Car.objects.values('brand').annotate(count=Count('id')).order_by('-count')[:8])
        
        # Payment status distribution
        payment_status_dist = list(Payment.objects.values('payment_status').annotate(count=Count('id')))
        
        # Monthly Sales Trend (last 6 months)
        monthly_sales_trend = []
        for i in range(5, -1, -1):
            month_date = today - timedelta(days=30*i)
            month_start = month_date.replace(day=1)
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year+1, month=1, day=1)
            else:
                month_end = month_start.replace(month=month_start.month+1, day=1)
            count = Sell.objects.filter(sell_date__gte=month_start, sell_date__lt=month_end).count()
            monthly_sales_trend.append({'month': month_start.strftime('%b'), 'count': count})
        
        # Monthly Revenue Trend
        monthly_revenue_trend = []
        for i in range(5, -1, -1):
            month_date = today - timedelta(days=30*i)
            month_start = month_date.replace(day=1)
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year+1, month=1, day=1)
            else:
                month_end = month_start.replace(month=month_start.month+1, day=1)
            revenue = Payment.objects.filter(payment_status='completed', payment_date__gte=month_start, payment_date__lt=month_end).aggregate(Sum('amount'))['amount__sum'] or 0
            monthly_revenue_trend.append({'month': month_start.strftime('%b'), 'revenue': float(revenue)})
        
        # Price Range Distribution
        price_ranges = [
            {'label': '< ₹5L', 'min': 0, 'max': 500000},
            {'label': '₹5-10L', 'min': 500000, 'max': 1000000},
            {'label': '₹10-20L', 'min': 1000000, 'max': 2000000},
            {'label': '₹20-50L', 'min': 2000000, 'max': 5000000},
            {'label': '₹50L+', 'min': 5000000, 'max': 100000000},
        ]
        price_distribution = []
        for pr in price_ranges:
            count = Car.objects.filter(price__gte=pr['min'], price__lt=pr['max']).count()
            price_distribution.append({'range': pr['label'], 'count': count})
        
        # Daily Inquiries (last 7 days)
        daily_inquiries = []
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            count = Inquiry.objects.filter(created_at__date=day).count()
            daily_inquiries.append({'day': day.strftime('%a'), 'count': count})
        
        # Recent Sells (for table)
        recent_sells = list(
            Sell.objects.select_related('car', 'customer')
            .order_by('-sell_date')[:5]
            .values('car__name', 'customer__name', 'sell_price', 'sell_date')
        )
        
        # Recent Payments (for table)
        recent_payments = list(
            Payment.objects.select_related('customer', 'car')
            .order_by('-payment_date')[:5]
            .values('customer__name', 'amount', 'payment_method', 'payment_status')
        )
        
        # Unresolved Inquiries (for table)
        recent_inquiries = list(
            Inquiry.objects.filter(is_resolved=False)
            .order_by('-created_at')[:5]
            .values('name', 'car__name', 'phone', 'created_at')
        )
        
        # Top Brands
        top_brands = list(
            Sell.objects.values('car__brand')
            .annotate(count=Count('id'))
            .order_by('-count')[:5]
        )
        
        return JsonResponse({
            'stats': {
                'total_cars': total_cars,
                'available_cars': available_cars,
                'sold_cars': sold_cars,
                'total_sells': total_sells,
                'total_customers': total_customers,
                'total_revenue': float(total_revenue),
                'pending_payments': pending_payments,
                'total_inquiries': total_inquiries,
                'unresolved_inquiries': unresolved_inquiries,
                'resolved_inquiries': resolved_inquiries,
                'monthly_sells': monthly_sells,
                'conversion_rate': conversion_rate,
            },
            'emi': {
                'active_emi': active_emi,
                'overdue_emi': overdue_emi,
                'emi_outstanding': emi_outstanding,
            },
            'cars_by_fuel': cars_by_fuel,
            'cars_by_brand': cars_by_brand,
            'payment_status_dist': payment_status_dist,
            'monthly_sales_trend': monthly_sales_trend,
            'monthly_revenue_trend': monthly_revenue_trend,
            'price_distribution': price_distribution,
            'daily_inquiries': daily_inquiries,
            'recent_sells': recent_sells,
            'recent_payments': recent_payments,
            'recent_inquiries': recent_inquiries,
            'top_brands': top_brands,
        }, encoder=DjangoJSONEncoder)
    
    def dashboard_view(self, request):
        today = timezone.now().date()
        last_30_days = today - timedelta(days=30)
        last_7_days = today - timedelta(days=7)
        
        # Basic stats
        total_cars = Car.objects.count()
        available_cars = Car.objects.filter(is_available=True).count()
        sold_cars = total_cars - available_cars
        total_customers = Customer.objects.count()
        total_sells = Sell.objects.count()
        total_revenue = Payment.objects.filter(payment_status='completed').aggregate(Sum('amount'))['amount__sum'] or 0
        pending_payments = Payment.objects.filter(payment_status='pending').count()
        total_inquiries = Inquiry.objects.count()
        unresolved_inquiries = Inquiry.objects.filter(is_resolved=False).count()
        
        # This month stats
        this_month_start = today.replace(day=1)
        monthly_sells = Sell.objects.filter(sell_date__gte=this_month_start).count()
        monthly_revenue = Payment.objects.filter(
            payment_status='completed', 
            payment_date__gte=this_month_start
        ).aggregate(Sum('amount'))['amount__sum'] or 0
        
        # Growth calculations (compare with last month)
        last_month_start = (this_month_start - timedelta(days=1)).replace(day=1)
        last_month_sells = Sell.objects.filter(
            sell_date__gte=last_month_start, 
            sell_date__lt=this_month_start
        ).count()
        sells_growth = ((monthly_sells - last_month_sells) / last_month_sells * 100) if last_month_sells > 0 else 0
        
        # Top selling brands
        top_brands = (
            Sell.objects.values('car__brand')
            .annotate(count=Count('id'))
            .order_by('-count')[:5]
        )
        
        # Recent activities
        recent_sells = Sell.objects.select_related('car', 'customer').order_by('-sell_date')[:5]
        recent_inquiries = Inquiry.objects.filter(is_resolved=False).order_by('-created_at')[:5]
        recent_payments = Payment.objects.select_related('customer', 'car').order_by('-payment_date')[:5]
        
        # Cars by fuel type for pie chart
        cars_by_fuel = list(Car.objects.values('fuel_type').annotate(count=Count('id')))
        
        # Cars by brand for bar chart
        cars_by_brand = list(Car.objects.values('brand').annotate(count=Count('id')).order_by('-count')[:8])
        
        # Payment status distribution
        payment_status_dist = list(Payment.objects.values('payment_status').annotate(count=Count('id')))
        
        active_emi = EMIPlan.objects.filter(plan_status='active').count()
        overdue_emi = EMIPlan.objects.filter(plan_status='active', next_due_date__lt=today).count()
        emi_outstanding = EMIPlan.objects.filter(plan_status='active').aggregate(total=Sum('loan_amount'))['total'] or 0
        finance_setting = FinanceSetting.get_solo()
        finance_setting_change_url = reverse('car_admin:car_financesetting_change', args=[finance_setting.pk])
        finance_setting_list_url = reverse('car_admin:car_financesetting_changelist')
        
        # ===== NEW: Essential Analytics Charts Data =====
        
        # Monthly Sales Trend (last 6 months)
        monthly_sales_trend = []
        for i in range(5, -1, -1):
            month_date = today - timedelta(days=30*i)
            month_start = month_date.replace(day=1)
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year+1, month=1, day=1)
            else:
                month_end = month_start.replace(month=month_start.month+1, day=1)
            count = Sell.objects.filter(sell_date__gte=month_start, sell_date__lt=month_end).count()
            monthly_sales_trend.append({
                'month': month_start.strftime('%b'),
                'count': count
            })
        
        # Monthly Revenue Trend (last 6 months)
        monthly_revenue_trend = []
        for i in range(5, -1, -1):
            month_date = today - timedelta(days=30*i)
            month_start = month_date.replace(day=1)
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year+1, month=1, day=1)
            else:
                month_end = month_start.replace(month=month_start.month+1, day=1)
            revenue = Payment.objects.filter(
                payment_status='completed',
                payment_date__gte=month_start,
                payment_date__lt=month_end
            ).aggregate(Sum('amount'))['amount__sum'] or 0
            monthly_revenue_trend.append({
                'month': month_start.strftime('%b'),
                'revenue': float(revenue)
            })
        
        # Price Range Distribution
        price_ranges = [
            {'label': '< ₹5L', 'min': 0, 'max': 500000},
            {'label': '₹5-10L', 'min': 500000, 'max': 1000000},
            {'label': '₹10-20L', 'min': 1000000, 'max': 2000000},
            {'label': '₹20-50L', 'min': 2000000, 'max': 5000000},
            {'label': '₹50L+', 'min': 5000000, 'max': 100000000},
        ]
        price_distribution = []
        for pr in price_ranges:
            count = Car.objects.filter(price__gte=pr['min'], price__lt=pr['max']).count()
            price_distribution.append({'range': pr['label'], 'count': count})
        
        # Inquiry to Sale Conversion
        total_resolved_inquiries = Inquiry.objects.filter(is_resolved=True).count()
        conversion_rate = (total_sells / total_resolved_inquiries * 100) if total_resolved_inquiries > 0 else 0
        
        # Recent 7 days trend
        daily_inquiries = []
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            count = Inquiry.objects.filter(created_at__date=day).count()
            daily_inquiries.append({'day': day.strftime('%a'), 'count': count})
        
        context = {
            **self.each_context(request),
            'title': 'Dashboard',
            'total_cars': total_cars,
            'available_cars': available_cars,
            'sold_cars': sold_cars,
            'total_customers': total_customers,
            'total_sells': total_sells,
            'total_revenue': total_revenue,
            'pending_payments': pending_payments,
            'total_inquiries': total_inquiries,
            'unresolved_inquiries': unresolved_inquiries,
            'monthly_sells': monthly_sells,
            'monthly_revenue': monthly_revenue,
            'sells_growth': round(sells_growth, 1),
            'top_brands': top_brands,
            'recent_sells': recent_sells,
            'recent_inquiries': recent_inquiries,
            'recent_payments': recent_payments,
            'cars_by_fuel': json.dumps(cars_by_fuel),
            'cars_by_brand': json.dumps(cars_by_brand),
            'payment_status_dist': json.dumps(payment_status_dist),
            'active_emi': active_emi,
            'overdue_emi': overdue_emi,
            'emi_outstanding': emi_outstanding,
            'default_interest_rate': finance_setting.default_interest_rate,
            'finance_setting_id': finance_setting.pk,
            'finance_setting_change_url': finance_setting_change_url,
            'finance_setting_list_url': finance_setting_list_url,
            # New analytics data
            'monthly_sales_trend': json.dumps(monthly_sales_trend),
            'monthly_revenue_trend': json.dumps(monthly_revenue_trend),
            'price_distribution': json.dumps(price_distribution),
            'conversion_rate': round(conversion_rate, 1),
            'daily_inquiries': json.dumps(daily_inquiries),
            'total_resolved_inquiries': total_resolved_inquiries,
        }
        return render(request, 'admin/dashboard.html', context)


admin_site = CarStoreAdminSite(name='car_admin')


class CarColorInline(admin.TabularInline):
    """Inline admin for car colors"""
    model = CarColor
    extra = 1
    fields = ['color_swatch', 'name', 'hex_code', 'stock', 'is_available', 'order', 'image_count']
    readonly_fields = ['color_swatch', 'image_count']
    ordering = ['order', 'name']
    
    def color_swatch(self, obj):
        if obj.hex_code:
            return format_html(
                '<div style="width: 30px; height: 30px; background: {}; border-radius: 5px; border: 1px solid #666;"></div>',
                obj.hex_code
            )
        return format_html('<span style="color: #9ca3af;">—</span>')
    color_swatch.short_description = 'Color'
    
    def image_count(self, obj):
        if obj.pk:
            count = obj.images.count()
            return format_html('<span style="color: #22c55e; font-weight: 600;">{}</span>', count)
        return '—'
    image_count.short_description = 'Images'


class CarImageInline(admin.TabularInline):
    """Inline admin for car gallery images"""
    model = CarImage
    extra = 1
    fields = ['image_preview', 'car_color', 'image', 'caption', 'is_primary', 'order']
    readonly_fields = ['image_preview']
    ordering = ['car_color', 'order', '-is_primary']
    
    def image_preview(self, obj):
        if obj.pk and obj.image:
            try:
                # Existing image - show preview with clickable link
                return format_html(
                    '<div style="display: flex; align-items: center; gap: 10px;">'
                    '<a href="{}" target="_blank"><img src="{}" width="80" height="50" style="object-fit: cover; border-radius: 5px;"/></a>'
                    '</div>',
                    obj.image.url, obj.image.url
                )
            except ValueError:
                pass
        return format_html('<span style="color: #9ca3af; font-size: 0.8rem;">No preview</span>')
    image_preview.short_description = 'Preview'
    
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "car_color":
            # Get the car ID from the URL
            if hasattr(request, '_obj_'):
                kwargs["queryset"] = CarColor.objects.filter(car=request._obj_)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Car, site=admin_site)
class CarAdmin(admin.ModelAdmin):
    list_display = ['name', 'brand', 'model_year', 'price_display', 'selling_price_display', 'emi_rate_display', 'fuel_type', 'total_stock_display', 'is_available', 'car_image', 'image_count']
    list_filter = ['brand', 'fuel_type', 'transmission', 'is_available', 'model_year']
    search_fields = ['name', 'brand', 'description']
    list_editable = ['is_available']
    list_per_page = 20
    ordering = ['-created_at']
    readonly_fields = ['image_preview', 'total_stock_display']
    inlines = [CarColorInline, CarImageInline]
    fieldsets = (
        ('Car Overview', {
            'fields': ('name', 'brand', 'model_year', 'is_available', 'total_stock_display')
        }),
        ('Pricing', {
            'fields': ('price', 'selling_price', 'emi_interest_rate')
        }),
        ('Specifications', {
            'fields': ('fuel_type', 'transmission', 'mileage', 'engine')
        }),
        ('Description', {
            'fields': ('description',)
        }),
    )
    
    def price_display(self, obj):
        value = obj.price
        if value >= 20000000:  # 2 Crores
            return f"₹{value / 10000000:.2f} Crores"
        elif value >= 10000000:  # 1 Crore
            return f"₹{value / 10000000:.2f} Crore"
        elif value >= 200000:  # 2 Lakhs
            return f"₹{value / 100000:.2f} Lakhs"
        elif value >= 100000:  # 1 Lakh
            return f"₹{value / 100000:.2f} Lakh"
        else:
            return f"₹{value:,.2f}"
    price_display.short_description = 'Price'
    
    def selling_price_display(self, obj):
        if obj.selling_price is None:
            return '—'
        value = obj.selling_price
        if value >= 20000000:  # 2 Crores
            return f"₹{value / 10000000:.2f} Crores"
        elif value >= 10000000:  # 1 Crore
            return f"₹{value / 10000000:.2f} Crore"
        elif value >= 200000:  # 2 Lakhs
            return f"₹{value / 100000:.2f} Lakhs"
        elif value >= 100000:  # 1 Lakh
            return f"₹{value / 100000:.2f} Lakh"
        else:
            return f"₹{value:,.2f}"
    selling_price_display.short_description = 'Selling Price'
    
    def car_image(self, obj):
        src = obj.image_src
        if not src:
            # Fallback for Admin: Try to find ANY image from colors
            first_color = obj.colors.first()
            if first_color:
                first_img = first_color.images.first()
                if first_img:
                    src = first_img.image.url
        
        if src:
            return format_html('<img src="{}" width="80" height="50" style="object-fit: cover; border-radius: 5px;"/>', src)
        return format_html('<span style="color: #9ca3af;">No Image</span>')
    car_image.short_description = 'Image'

    def image_preview(self, obj):
        if not obj:
            return "No image uploaded yet."
            
        src = getattr(obj, 'image_src', '')
        if not src:
            # Fallback for Admin
            first_color = obj.colors.first()
            if first_color:
                first_img = first_color.images.first()
                if first_img:
                    src = first_img.image.url

        if src:
            return format_html('<img src="{}" style="max-width: 240px; border-radius: 8px;"/>', src)
        return "No image uploaded yet."
    image_preview.short_description = 'Preview'

    def emi_rate_display(self, obj):
        if obj.emi_interest_rate is None:
            return '—'
        return f"{obj.emi_interest_rate:.2f}%"
    emi_rate_display.short_description = 'EMI Rate'
    
    def image_count(self, obj):
        count = obj.images.count()
        if count == 0:
            return format_html('<span style="color: #9ca3af;">0</span>')
        return format_html('<span style="color: #22c55e; font-weight: 600;">{}</span>', count)
    image_count.short_description = 'Photos'
    
    def total_stock_display(self, obj):
        """Display total stock calculated from all color variants."""
        if not obj or not obj.pk:
            return '—'
        total = obj.total_stock
        if total == 0:
            return format_html('<span style="color: #ef4444; font-weight: 600;">0</span>')
        return format_html('<span style="color: #22c55e; font-weight: 600;">{}</span>', total)
    total_stock_display.short_description = 'Total Stock'
    
    def get_form(self, request, obj=None, **kwargs):
        # Store the object for use in inline filtering
        request._obj_ = obj
        return super().get_form(request, obj, **kwargs)


@admin.register(Customer, site=admin_site)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'phone', 'created_at']
    search_fields = ['name', 'email', 'phone']
    list_filter = ['created_at']
    readonly_fields = ['created_at']
    actions = [export_as_csv]
    fieldsets = (
        ('Personal Information', {
            'fields': ('user', 'name')
        }),
        ('Contact Details', {
            'fields': ('email', 'phone', 'address'),
            'classes': ('collapse',),
            'description': 'Contact information for communication.'
        }),
        ('System Info', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )


@admin.register(Payment, site=admin_site)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['customer', 'car', 'amount_display', 'payment_method', 'payment_status', 'payment_date']
    list_filter = ['payment_method', 'payment_status', 'payment_date']
    search_fields = ['customer__name', 'transaction_id', 'stripe_session_id', 'car__name']
    readonly_fields = ['transaction_id', 'stripe_session_id', 'payment_date']
    actions = [export_as_csv]
    
    fieldsets = (
        ('Payment Summary', {
            'fields': ('customer', 'car', 'amount')
        }),
        ('Transaction Details', {
            'fields': ('payment_method', 'payment_status', 'transaction_id'),
            'description': 'Method and status of the transaction.'
        }),
        ('System Data', {
            'fields': ('stripe_session_id', 'payment_date', 'car_color'),
            'classes': ('collapse',)
        }),
    )

    def amount_display(self, obj):
        return f"₹{obj.amount:,.2f}"
    amount_display.short_description = 'Amount'


@admin.register(EMIPlan, site=admin_site)
class EMIPlanAdmin(admin.ModelAdmin):
    list_display = ['customer', 'car', 'monthly_emi_display', 'tenure_months', 'plan_status', 'next_due_date']
    list_filter = ['plan_status', 'tenure_months', 'interest_rate', 'next_due_date']
    search_fields = ['customer__name', 'customer__email', 'car__name']
    readonly_fields = ['created_at', 'updated_at', 'start_date']
    actions = [export_as_csv, 'mark_completed', 'mark_defaulted']
    fieldsets = (
        ('Plan Details', {
            'fields': ('customer', 'car', 'payment', 'plan_status')
        }),
        ('Financials', {
            'fields': ('down_payment', 'loan_amount', 'interest_rate', 'tenure_months', 'monthly_emi', 'total_interest', 'total_payable')
        }),
        ('Schedule', {
            'fields': ('start_date', 'next_due_date')
        }),
        ('Meta', {
            'fields': ('created_at', 'updated_at')
        }),
    )
    actions = ['mark_completed', 'mark_defaulted']
    date_hierarchy = 'start_date'

    def monthly_emi_display(self, obj):
        return f"₹{obj.monthly_emi:,.2f}"
    monthly_emi_display.short_description = 'Monthly EMI'

    @admin.action(description='Mark selected EMI plans as completed')
    def mark_completed(self, request, queryset):
        updated = queryset.update(plan_status='completed')
        self.message_user(request, f"{updated} EMI plans marked as completed.")

    @admin.action(description='Mark selected EMI plans as defaulted')
    def mark_defaulted(self, request, queryset):
        updated = queryset.update(plan_status='defaulted')
        self.message_user(request, f"{updated} EMI plans marked as defaulted.")


@admin.register(Sell, site=admin_site)
class SellAdmin(admin.ModelAdmin):
    list_display = ['car', 'customer', 'sell_price_display', 'sell_date']
    list_filter = ['sell_date']
    search_fields = ['car__name', 'customer__name', 'customer__email']
    readonly_fields = ['sell_date']
    actions = [export_as_csv]
    
    fieldsets = (
        ('Sale Information', {
            'fields': ('car', 'customer', 'sell_price')
        }),
        ('Vehicle Details', {
            'fields': ('car_color',),
            'description': 'Specific variant sold.'
        }),
        ('Timestamp', {
            'fields': ('sell_date',),
            'classes': ('collapse',)
        }),
    )

    def sell_price_display(self, obj):
        return f"₹{obj.sell_price:,.2f}"
    sell_price_display.short_description = 'Sale Price'


@admin.register(Inquiry, site=admin_site)
class InquiryAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'phone', 'car', 'is_resolved', 'created_at']
    list_filter = ['is_resolved', 'created_at']
    search_fields = ['name', 'email', 'car__name']
    list_editable = ['is_resolved']
    readonly_fields = ['created_at']
    actions = [export_as_csv]
    
    fieldsets = (
        ('Inquirer Info', {
            'fields': ('name', 'email', 'phone')
        }),
        ('Inquiry Details', {
            'fields': ('car', 'message', 'is_resolved', 'created_at')
        }),
    )


@admin.register(CarReview, site=admin_site)
class CarReviewAdmin(admin.ModelAdmin):
    list_display = ['car', 'user', 'rating', 'title', 'created_at']
    list_filter = ['rating', 'created_at', 'car']
    search_fields = ['car__name', 'car__brand', 'user__username', 'title', 'comment']
    autocomplete_fields = ['car', 'user']
    readonly_fields = ['created_at', 'updated_at']
    actions = [export_as_csv]
    
    fieldsets = (
        ('Review', {
            'fields': ('car', 'user', 'rating', 'title', 'comment')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(FinanceSetting, site=admin_site)
class FinanceSettingAdmin(admin.ModelAdmin):
    list_display = ['default_interest_rate', 'updated_at']
    readonly_fields = ['updated_at', 'singleton_key']
    actions = [export_as_csv]
    fieldsets = (
        (None, {
            'fields': ('default_interest_rate',)
        }),
        ('System', {
            'classes': ('collapse',),
            'fields': ('updated_at', 'singleton_key')
        })
    )

    def has_add_permission(self, request):
        if FinanceSetting.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ContactInfo, site=admin_site)
class ContactInfoAdmin(admin.ModelAdmin):
    list_display = ['address', 'phone_1', 'email_1', 'updated_at']
    readonly_fields = ['singleton_key', 'updated_at']
    
    fieldsets = (
        ('Contact Details', {
            'fields': ('address', 'phone_1', 'phone_2', 'email_1', 'email_2')
        }),
        ('Store Info', {
            'fields': ('working_hours', 'map_embed_url')
        }),
        ('System', {
            'fields': ('updated_at', 'singleton_key'),
            'classes': ('collapse',)
        })
    )

    def has_add_permission(self, request):
        if ContactInfo.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TestDrive, site=admin_site)
class TestDriveAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'car', 'preferred_date', 'preferred_time', 'status', 'phone', 'created_at']
    list_filter = ['status', 'preferred_date', 'created_at']
    search_fields = ['full_name', 'email', 'phone', 'car__name', 'car__brand', 'driving_license']
    list_editable = ['status']
    readonly_fields = ['created_at', 'updated_at']
    autocomplete_fields = ['car', 'user']
    date_hierarchy = 'preferred_date'
    ordering = ['-created_at']
    actions = [export_as_csv, 'mark_confirmed', 'mark_completed', 'mark_cancelled']
    
    fieldsets = (
        ('Test Drive Request', {
            'fields': ('user', 'car', 'status')
        }),
        ('Contact Information', {
            'fields': ('full_name', 'email', 'phone', 'driving_license', 'address')
        }),
        ('Schedule', {
            'fields': ('preferred_date', 'preferred_time')
        }),
        ('Additional Info', {
            'fields': ('message',),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    actions = ['mark_confirmed', 'mark_completed', 'mark_cancelled']
    
    def mark_confirmed(self, request, queryset):
        queryset.update(status='confirmed')
        self.message_user(request, f'{queryset.count()} test drive(s) marked as confirmed.')
    mark_confirmed.short_description = 'Mark selected as Confirmed'
    
    def mark_completed(self, request, queryset):
        queryset.update(status='completed')
        self.message_user(request, f'{queryset.count()} test drive(s) marked as completed.')
    mark_completed.short_description = 'Mark selected as Completed'
    
    def mark_cancelled(self, request, queryset):
        queryset.update(status='cancelled')
        self.message_user(request, f'{queryset.count()} test drive(s) marked as cancelled.')
    mark_cancelled.short_description = 'Mark selected as Cancelled'


# Also register with default admin
admin.site.register(Car)
admin.site.register(Customer)
admin.site.register(Payment)
admin.site.register(Sell)
admin.site.register(Inquiry)
admin.site.register(CarReview)
admin.site.register(FinanceSetting)
admin.site.register(TestDrive)

# Register User and Group with custom admin site
admin_site.register(User, UserAdmin)
admin_site.register(Group, GroupAdmin)


# Admin History Log
class LogEntryAdmin(admin.ModelAdmin):
    list_display = ['action_time', 'user', 'content_type', 'object_repr', 'action_flag_display', 'change_message']
    list_filter = ['action_time', 'user', 'content_type', 'action_flag']
    search_fields = ['object_repr', 'change_message', 'user__username']
    date_hierarchy = 'action_time'
    ordering = ['-action_time']
    readonly_fields = ['action_time', 'user', 'content_type', 'object_id', 'object_repr', 'action_flag', 'change_message']
    actions = [export_as_csv]
    
    def action_flag_display(self, obj):
        flags = {
            1: format_html('<span style="color: green; font-weight: bold;">➕ Added</span>'),
            2: format_html('<span style="color: orange; font-weight: bold;">✏️ Changed</span>'),
            3: format_html('<span style="color: red; font-weight: bold;">🗑️ Deleted</span>'),
        }
        return flags.get(obj.action_flag, obj.action_flag)
    action_flag_display.short_description = 'Action'
    
    def has_add_permission(self, request):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


admin_site.register(LogEntry, LogEntryAdmin)


class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'subject', 'is_read', 'created_at']
    list_filter = ['subject', 'is_read', 'created_at']
    search_fields = ['name', 'email', 'message']
    readonly_fields = ['name', 'email', 'phone', 'subject', 'message', 'created_at']
    list_editable = ['is_read']
    ordering = ['-created_at']
    actions = [export_as_csv]
    
    def has_add_permission(self, request):
        return False

admin_site.register(ContactMessage, ContactMessageAdmin)


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'Profile'
    fields = ['photo', 'bio', 'phone']


class CustomUserAdmin(UserAdmin):
    inlines = [UserProfileInline]
    list_display = ['username', 'email', 'first_name', 'last_name', 'is_staff', 'is_active', 'get_photo']
    actions = [export_as_csv]
    
    def get_photo(self, obj):
        if hasattr(obj, 'profile') and obj.profile.photo:
            return format_html('<img src="{}" width="30" height="30" style="border-radius: 50%; object-fit: cover;" />', obj.profile.photo.url)
        return format_html('<span style="color: #999;">No photo</span>')
    get_photo.short_description = 'Photo'

# Unregister the default User admin and register with custom one
admin_site.unregister(User)
admin_site.register(User, CustomUserAdmin)


class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'get_photo', 'phone']
    search_fields = ['user__username', 'user__email']
    actions = [export_as_csv]
    
    def get_photo(self, obj):
        if obj.photo:
            return format_html('<img src="{}" width="40" height="40" style="border-radius: 50%; object-fit: cover;" />', obj.photo.url)
        return format_html('<span style="color: #999;">No photo</span>')
    get_photo.short_description = 'Photo'

admin_site.register(UserProfile, UserProfileAdmin)


# Notification Admin with Send Form
class NotificationAdminForm(forms.ModelForm):
    send_to = forms.ChoiceField(
        choices=[('all', 'All Users'), ('specific', 'Specific User')],
        initial='all',
        widget=forms.RadioSelect
    )
    
    class Meta:
        model = Notification
        fields = ['title', 'message', 'notification_type', 'link', 'user']
        widgets = {
            'message': forms.Textarea(attrs={'rows': 4}),
        }


class NotificationAdmin(admin.ModelAdmin):
    form = NotificationAdminForm
    list_display = ['title', 'notification_type_badge', 'recipient', 'is_read', 'created_at', 'created_by']
    list_filter = ['notification_type', 'is_global', 'is_read', 'created_at']
    search_fields = ['title', 'message', 'user__username']
    readonly_fields = ['created_at', 'created_by']
    ordering = ['-created_at']
    actions = [export_as_csv]
    
    fieldsets = (
        ('Message Content', {
            'fields': ('title', 'message', 'notification_type', 'link')
        }),
        ('Recipient', {
            'fields': ('send_to', 'user'),
            'description': 'Select "All Users" to send to everyone, or "Specific User" and choose a user.'
        }),
    )
    
    class Media:
        js = ('admin/js/notification_admin.js',)
    
    def notification_type_badge(self, obj):
        colors = {
            'info': '#3b82f6',
            'success': '#22c55e',
            'warning': '#f59e0b',
            'alert': '#ef4444',
            'promo': '#8b5cf6',
        }
        color = colors.get(obj.notification_type, '#6b7280')
        return format_html(
            '<span style="background: {}; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem;">{}</span>',
            color, obj.get_notification_type_display()
        )
    notification_type_badge.short_description = 'Type'
    
    def recipient(self, obj):
        if obj.is_global:
            return format_html('<span style="color: #fbbf24; font-weight: 500;">📢 All Users</span>')
        return obj.user.username if obj.user else '-'
    recipient.short_description = 'Recipient'
    
    def save_model(self, request, obj, form, change):
        if not change:  # Only on create
            obj.created_by = request.user
            send_to = form.cleaned_data.get('send_to', 'all')
            if send_to == 'all':
                obj.is_global = True
                obj.user = None
            else:
                obj.is_global = False
        super().save_model(request, obj, form, change)
    
    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is None:  # Adding new
            form.base_fields['user'].required = False
        return form

admin_site.register(Notification, NotificationAdmin)
