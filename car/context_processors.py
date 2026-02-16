from .models import ContactInfo

def contact_info(request):
    """
    Context processor to make contact information available in all templates.
    """
    try:
        contact = ContactInfo.get_solo()
    except Exception:
        contact = None
    
    return {'contact_info': contact}
