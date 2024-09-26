from django.shortcuts import render
from django.http import HttpResponse
from .models import *

def home(request):
    orders = Order.objects.all()
    customers = Customer.objects.all()

    total_customers = customers.count()
    total_orders = orders.count()
    
    pending = orders.filter(status='Pending').count()
    delivered = orders.filter(status='Delivered').count()
	

    contex = {'customers':customers, 'orders':orders, 'total_orders':total_orders,
    'delivered':delivered, 'pending':pending}
    return render(request,'accounts\dashboard.html',contex)
	
def product(request):
    products = Product.objects.all()
    return render(request,'accounts/products.html',{'products':products})

def customer(request):
    return render(request,'accounts/customers.html')