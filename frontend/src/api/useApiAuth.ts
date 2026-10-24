import { useQuery } from '@tanstack/react-query'
import { apiGet } from './client'

export function useApiAuth() {
  return useQuery({
    queryKey: ['api-auth-status'],
    queryFn: () => apiGet<{ required: boolean }>('/api/v1/auth/status'),
    staleTime: 30000,
    refetchInterval: 30000,
    retry: false,
  })
}
