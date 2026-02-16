from . import views
from django.urls import path

urlpatterns = [
    path("", views.home, name='home'),
    path("cars/", views.car_list, name='car_list'),
    path("car/<int:car_id>/", views.car_detail, name='car_detail'),
    path("car/<int:car_id>/inquiry/", views.inquiry, name='inquiry'),
    path("car/<int:car_id>/test-drive/", views.test_drive, name='test_drive'),
    path("test-drive/confirmation/<int:booking_id>/", views.test_drive_confirmation, name='test_drive_confirmation'),
    path("car/<int:car_id>/payment/", views.make_payment, name='make_payment'),
    path("car/<int:car_id>/review/", views.submit_review, name='submit_review'),
    path("payment-success/<int:payment_id>/", views.payment_success, name='payment_success'),
    path("emi-plan/<int:plan_id>/", views.emi_plan_detail, name='emi_plan_detail'),
    path("make-emi-payment/", views.make_emi_payment, name='make_emi_payment'),
    path("register/", views.user_register, name='register'),
    path("login/", views.user_login, name='login'),
    path("logout/", views.user_logout, name='logout'),
    path("profile/", views.profile, name='profile'),
    path("profile/edit/", views.edit_profile, name='edit_profile'),
    path("profile/delete/", views.delete_account, name='delete_account'),
    path("about/", views.about, name='about'),
    path("contact/", views.contact, name='contact'),
    path("notifications/", views.notifications_list, name='notifications_list'),
    path("notifications/mark-read/<int:notification_id>/", views.mark_notification_read, name='mark_notification_read'),
    path("notifications/mark-all-read/", views.mark_all_notifications_read, name='mark_all_notifications_read'),
    path("api/calculate-emi/", views.calculate_emi, name='calculate_emi'),
    path("api/notifications/", views.api_notifications, name='api_notifications'),
    # API endpoints
    path("api/sales-data/", views.api_sales_data, name='api_sales_data'),
    path("api/brand-distribution/", views.api_brand_distribution, name='api_brand_distribution'),
    # Stripe Payment
    path("stripe/checkout/<int:car_id>/", views.create_stripe_checkout, name='create_stripe_checkout'),
    path("stripe/success/", views.stripe_success, name='stripe_success'),
    path("stripe/cancel/", views.stripe_cancel, name='stripe_cancel'),
    
    # EMI Stripe Payment
    path("stripe/emi-checkout/<int:plan_id>/", views.create_emi_stripe_checkout, name='create_emi_stripe_checkout'),
    path("stripe/emi-success/", views.stripe_emi_success, name='stripe_emi_success'),
]