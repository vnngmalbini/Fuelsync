from django.conf import settings
from supabase import create_client, Client

# Service role client — bypasses Row Level Security, for server-side operations
def get_admin_client() -> Client:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

# Anon client — respects Row Level Security, for public/user-facing operations
def get_anon_client() -> Client:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
