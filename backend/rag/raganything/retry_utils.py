"""
Retry utilities for RAGAnything

Provides retry decorators and utilities for handling transient failures
in LLM calls, network requests, and database operations.
"""

import asyncio
import functools
from typing import Any, Callable, Optional, Tuple, Type

from tenacity import (
    AsyncRetrying,
    RetryError,
    Retrying,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    retry_if_exception_type,
)

from lightrag.utils import logger


RETRYABLE_NETWORK_EXCEPTIONS: Tuple[Type[Exception], ...] = (
    ConnectionError,
    ConnectionRefusedError,
    ConnectionResetError,
    TimeoutError,
    asyncio.TimeoutError,
)

RETRYABLE_HTTP_STATUS_CODES: Tuple[int, ...] = (408, 429, 500, 502, 503, 504)


class RetryableError(Exception):
    """Exception that indicates a retryable error condition."""
    pass


class NonRetryableError(Exception):
    """Exception that indicates a non-retryable error condition."""
    pass


def is_retryable_http_error(error: Exception) -> bool:
    """Check if an HTTP error is retryable based on status code."""
    if hasattr(error, 'response') and hasattr(error.response, 'status_code'):
        return error.response.status_code in RETRYABLE_HTTP_STATUS_CODES
    if hasattr(error, 'status_code'):
        return error.status_code in RETRYABLE_HTTP_STATUS_CODES
    return False


def is_rate_limit_error(error: Exception) -> bool:
    """Check if error is a rate limit error."""
    error_str = str(error).lower()
    rate_limit_indicators = [
        'rate limit',
        'too many requests',
        '429',
        'quota exceeded',
        'requests per minute',
        'rpm',
        'tpm',
        'tokens per minute',
    ]
    return any(indicator in error_str for indicator in rate_limit_indicators)


def is_timeout_error(error: Exception) -> bool:
    """Check if error is a timeout error."""
    error_str = str(error).lower()
    timeout_indicators = ['timeout', 'timed out', 'deadline exceeded']
    return (
        isinstance(error, (TimeoutError, asyncio.TimeoutError)) or
        any(indicator in error_str for indicator in timeout_indicators)
    )


def should_retry(error: Exception) -> bool:
    """
    Determine if an error should trigger a retry.
    
    Args:
        error: The exception that occurred
        
    Returns:
        True if the error is retryable, False otherwise
    """
    if isinstance(error, NonRetryableError):
        return False
    
    if isinstance(error, RetryableError):
        return True
    
    if isinstance(error, RETRYABLE_NETWORK_EXCEPTIONS):
        return True
    
    if is_rate_limit_error(error):
        return True
    
    if is_timeout_error(error):
        return True
    
    if is_retryable_http_error(error):
        return True
    
    return False


def create_retry_decorator(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 30.0,
    exponential_multiplier: float = 1.0,
    retryable_exceptions: Optional[Tuple[Type[Exception], ...]] = None,
) -> Callable:
    """
    Create a retry decorator with specified parameters.
    
    Args:
        max_attempts: Maximum number of retry attempts
        min_wait: Minimum wait time between retries in seconds
        max_wait: Maximum wait time between retries in seconds
        exponential_multiplier: Multiplier for exponential backoff
        retryable_exceptions: Tuple of exception types to retry on
        
    Returns:
        A retry decorator function
    """
    if retryable_exceptions is None:
        retryable_exceptions = (Exception,)
    
    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                retryer = AsyncRetrying(
                    stop=stop_after_attempt(max_attempts),
                    wait=wait_exponential(
                        multiplier=exponential_multiplier,
                        min=min_wait,
                        max=max_wait,
                    ),
                    retry=retry_if_exception_type(retryable_exceptions),
                    before_sleep=before_sleep_log(logger, log_level=20),
                    reraise=True,
                )
                try:
                    return await retryer(func, *args, **kwargs)
                except RetryError as e:
                    logger.error(f"All {max_attempts} retry attempts failed for {func.__name__}: {e.last_attempt.exception()}")
                    raise e.last_attempt.exception()
            
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs) -> Any:
                retryer = Retrying(
                    stop=stop_after_attempt(max_attempts),
                    wait=wait_exponential(
                        multiplier=exponential_multiplier,
                        min=min_wait,
                        max=max_wait,
                    ),
                    retry=retry_if_exception_type(retryable_exceptions),
                    before_sleep=before_sleep_log(logger, log_level=20),
                    reraise=True,
                )
                try:
                    return retryer(func, *args, **kwargs)
                except RetryError as e:
                    logger.error(f"All {max_attempts} retry attempts failed for {func.__name__}: {e.last_attempt.exception()}")
                    raise e.last_attempt.exception()
            
            return sync_wrapper
    
    return decorator


def with_retry(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 30.0,
    exponential_multiplier: float = 1.0,
) -> Callable:
    """
    Decorator that adds retry logic to functions.
    
    Automatically retries on:
    - Network errors (ConnectionError, TimeoutError, etc.)
    - Rate limit errors (429 status, quota exceeded)
    - Server errors (500, 502, 503, 504)
    - RetryableError exceptions
    
    Args:
        max_attempts: Maximum number of retry attempts
        min_wait: Minimum wait time between retries in seconds
        max_wait: Maximum wait time between retries in seconds
        exponential_multiplier: Multiplier for exponential backoff
        
    Returns:
        Decorated function with retry logic
    """
    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                last_exception = None
                for attempt in range(max_attempts):
                    try:
                        return await func(*args, **kwargs)
                    except NonRetryableError as e:
                        logger.error(f"Non-retryable error in {func.__name__}: {e}")
                        raise
                    except Exception as e:
                        last_exception = e
                        if not should_retry(e):
                            logger.error(f"Non-retryable error in {func.__name__}: {e}")
                            raise
                        
                        if attempt < max_attempts - 1:
                            wait_time = min(
                                exponential_multiplier * (2 ** attempt),
                                max_wait
                            )
                            wait_time = max(wait_time, min_wait)
                            logger.warning(
                                f"Retry attempt {attempt + 1}/{max_attempts} for {func.__name__} "
                                f"after error: {e}. Waiting {wait_time:.1f}s before retry."
                            )
                            await asyncio.sleep(wait_time)
                        else:
                            logger.error(
                                f"All {max_attempts} retry attempts failed for {func.__name__}: {e}"
                            )
                            raise
                raise last_exception
            
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs) -> Any:
                import time
                last_exception = None
                for attempt in range(max_attempts):
                    try:
                        return func(*args, **kwargs)
                    except NonRetryableError as e:
                        logger.error(f"Non-retryable error in {func.__name__}: {e}")
                        raise
                    except Exception as e:
                        last_exception = e
                        if not should_retry(e):
                            logger.error(f"Non-retryable error in {func.__name__}: {e}")
                            raise
                        
                        if attempt < max_attempts - 1:
                            wait_time = min(
                                exponential_multiplier * (2 ** attempt),
                                max_wait
                            )
                            wait_time = max(wait_time, min_wait)
                            logger.warning(
                                f"Retry attempt {attempt + 1}/{max_attempts} for {func.__name__} "
                                f"after error: {e}. Waiting {wait_time:.1f}s before retry."
                            )
                            time.sleep(wait_time)
                        else:
                            logger.error(
                                f"All {max_attempts} retry attempts failed for {func.__name__}: {e}"
                            )
                            raise
                raise last_exception
            
            return sync_wrapper
    
    return decorator


async def retry_async_call(
    func: Callable,
    *args,
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 30.0,
    exponential_multiplier: float = 1.0,
    **kwargs,
) -> Any:
    """
    Execute an async function with retry logic.
    
    Args:
        func: Async function to execute
        *args: Positional arguments for the function
        max_attempts: Maximum number of retry attempts
        min_wait: Minimum wait time between retries in seconds
        max_wait: Maximum wait time between retries in seconds
        exponential_multiplier: Multiplier for exponential backoff
        **kwargs: Keyword arguments for the function
        
    Returns:
        Result of the function call
        
    Raises:
        Last exception if all retries fail
    """
    last_exception = None
    for attempt in range(max_attempts):
        try:
            return await func(*args, **kwargs)
        except NonRetryableError as e:
            logger.error(f"Non-retryable error in {func.__name__}: {e}")
            raise
        except Exception as e:
            last_exception = e
            if not should_retry(e):
                logger.error(f"Non-retryable error in {func.__name__}: {e}")
                raise
            
            if attempt < max_attempts - 1:
                wait_time = min(
                    exponential_multiplier * (2 ** attempt),
                    max_wait
                )
                wait_time = max(wait_time, min_wait)
                logger.warning(
                    f"Retry attempt {attempt + 1}/{max_attempts} for {func.__name__} "
                    f"after error: {e}. Waiting {wait_time:.1f}s before retry."
                )
                await asyncio.sleep(wait_time)
            else:
                logger.error(
                    f"All {max_attempts} retry attempts failed for {func.__name__}: {e}"
                )
                raise
    raise last_exception
