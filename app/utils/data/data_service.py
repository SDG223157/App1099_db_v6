# app/data/data_service.p

import yfinance as yf
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from dateutil.relativedelta import relativedelta
from app.utils.config.metrics_config import METRICS_MAP, CAGR_METRICS
from sqlalchemy import create_engine, inspect, text
import os
import logging
import re
from app.utils.visualization.visualization_service import is_stock

from time import sleep
from functools import wraps
import random
import time
import traceback

# Configure logger
logger = logging.getLogger(__name__)

def retry_with_backoff(retries=3, backoff_in_seconds=1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            x = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if x == retries:
                        raise
                    sleep_time = (backoff_in_seconds * 2 ** x +
                                random.uniform(0, 1))
                    sleep(sleep_time)
                    x += 1
        return wrapper
    return decorator

class RateLimiter:
    def __init__(self, calls_per_second=2):
        self.calls_per_second = calls_per_second
        self.last_call_time = 0

    def wait(self):
        current_time = time.time()
        time_since_last_call = current_time - self.last_call_time
        time_to_wait = (1.0 / self.calls_per_second) - time_since_last_call
        
        if time_to_wait > 0:
            sleep(time_to_wait)
            
        self.last_call_time = time.time()

class DataService:
    def __init__(self):
        """Initialize DataService with API and database configuration"""
        self.API_KEY = "a365bff224a6419fac064dd52e1f80d9"
        self.BASE_URL = "https://api.roic.ai/v1/rql"
        self.METRICS = METRICS_MAP
        self.CAGR_METRICS = CAGR_METRICS
        
        # Database configuration
        self.engine = create_engine(
            f"mysql+pymysql://{os.getenv('MYSQL_USER')}:"
            f"{os.getenv('MYSQL_PASSWORD')}@"
            f"{os.getenv('MYSQL_HOST')}:"
            f"{os.getenv('MYSQL_PORT', '3306')}/"
            f"{os.getenv('MYSQL_DATABASE')}"
        )

    def table_exists(self, table_name: str) -> bool:
        """Check if table exists in database"""
        try:
            inspector = inspect(self.engine)
            return table_name in inspector.get_table_names()
        except Exception as e:
            print(f"Error checking table existence: {e}")
            return False

    def store_dataframe(self, df: pd.DataFrame, table_name: str) -> bool:
        """Store DataFrame in database"""
        try:
            df.to_sql(
                name=table_name,
                con=self.engine,
                index=True,
                if_exists='replace',
                chunksize=10000
            )
            print(f"Successfully stored data in table: {table_name}")
            return True
        except Exception as e:
            print(f"Error storing DataFrame in table {table_name}: {e}")
            return False
    def clean_ticker_for_table_name(self, ticker: str) -> str:
        """
        Clean ticker symbol for use in table name.
        Removes special characters and converts to valid table name format.
        
        Parameters:
        -----------
        ticker : str
            Original ticker symbol
        
        Returns:
        --------
        str
            Cleaned ticker symbol safe for use in table names
        """
        # Replace any non-alphanumeric characters with underscore
        cleaned = ''.join(c if c.isalnum() else '_' for c in ticker)
        # Remove leading/trailing underscores
        cleaned = cleaned.strip('_')
        # Convert to lowercase
        cleaned = cleaned.lower()
        # If the cleaned string is empty, use a default
        if not cleaned:
            cleaned = 'unknown'
        return cleaned
    
    
    
    def get_historical_data(self, ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Get historical data from MySQL database or yfinance.
        Updates database with new data if requested end date is beyond max date.
        
        Parameters:
        -----------
        ticker : str
            Stock ticker symbol
        start_date : str
            Start date in YYYY-MM-DD format
        end_date : str
            End date in YYYY-MM-DD format
        
        Returns:
        --------
        pd.DataFrame
            DataFrame containing historical price data for the requested date range
        """
        cleaned_ticker = self.clean_ticker_for_table_name(ticker)
        table_name = f"his_{cleaned_ticker}"
        
        try:
            # Get the latest trading day (last Friday if weekend)
            latest_trading_day = pd.Timestamp.now()
            while latest_trading_day.weekday() > 4:  # 5 = Saturday, 6 = Sunday
                latest_trading_day -= pd.Timedelta(days=1)
            latest_trading_day = latest_trading_day.strftime('%Y-%m-%d')
            
            # Adjust end_date if it's beyond latest trading day
            end_date = min(pd.to_datetime(end_date), pd.to_datetime(latest_trading_day)).strftime('%Y-%m-%d')
            
            # Check if table exists in database
            if self.table_exists(table_name):
                logging.info(f"Getting historical data for {ticker} from database")
                
                # Get table's date range
                date_range_query = text(f"""
                    SELECT MIN(Date) as min_date, MAX(Date) as max_date 
                    FROM {table_name}
                """)
                date_range = pd.read_sql_query(date_range_query, self.engine)
                
                # Check for None in date_range values
                min_date = date_range['min_date'][0]
                max_date = date_range['max_date'][0]
                
                # If min_date or max_date is None, refresh all data
                if min_date is None or max_date is None:
                    logging.info(f"Database date range is invalid for {ticker}: min_date={min_date}, max_date={max_date}")
                    logging.info("Refreshing data from external source...")
                    success = self.store_historical_data(ticker)
                    if not success:
                        raise ValueError(f"Failed to store data for {ticker}")
                    df = pd.read_sql_table(table_name, self.engine)
                    df.set_index('Date', inplace=True)
                    return df[(df.index >= start_date) & (df.index <= end_date)]
                
                # Convert dates for comparison
                db_start = pd.to_datetime(min_date).strftime('%Y-%m-%d')
                db_end = pd.to_datetime(max_date).strftime('%Y-%m-%d')
                current_date = pd.Timestamp.now().strftime('%Y-%m-%d')
                
                # Calculate default 10-year period
                default_start = (pd.Timestamp.now() - pd.DateOffset(years=10)).strftime('%Y-%m-%d')
                
                # If requested start date is before both database start and default period
                if (start_date < db_start) and (start_date < default_start):
                    logging.info(f"Requested start date {start_date} is before database range and default period")
                    logging.info("Fetching data directly from yfinance...")
                    
                    # Fetch data directly for the requested period
                    ticker_obj = yf.Ticker(ticker)
                    df = ticker_obj.history(start=start_date, end=end_date)
                    if df.empty:
                        raise ValueError(f"No data found for {ticker} in requested date range")
                    df.index = df.index.tz_localize(None)
                    return df

                # If requested start date is before database start but within default period
                if start_date < db_start:
                    logging.info(f"Requested start date {start_date} is before database start date {db_start}")
                    logging.info("Fetching additional historical data from yfinance...")
                    
                    # Fetch additional historical data
                    ticker_obj = yf.Ticker(ticker)
                    new_data = ticker_obj.history(start=start_date, end=db_start)
                    new_data.index = new_data.index.tz_localize(None)
                    
                    # Read existing data
                    existing_data = pd.read_sql_table(table_name, self.engine)
                    existing_data.set_index('Date', inplace=True)
                    
                    # Combine datasets
                    combined_data = pd.concat([new_data, existing_data])
                    combined_data = combined_data[~combined_data.index.duplicated(keep='last')]
                    combined_data.sort_index(inplace=True)
                    
                    # Update database
                    success = self.store_dataframe(combined_data, table_name)
                    if not success:
                        raise ValueError(f"Failed to update data for {ticker}")
                    
                    return combined_data[(combined_data.index >= start_date) & (combined_data.index <= end_date)]
                
                # Check if data needs updating (more than 10 days old)
                days_difference = (pd.to_datetime(current_date) - pd.to_datetime(db_end)).days
                if days_difference > 10:
                    logging.info(f"Data is {days_difference} days old. Updating from yfinance...")
                    
                    # Delete the last 10 days of data
                    cutoff_date = pd.to_datetime(db_end) - pd.Timedelta(days=10)
                    
                    # Read existing data
                    existing_data = pd.read_sql_table(table_name, self.engine)
                    existing_data.set_index('Date', inplace=True)
                    existing_data = existing_data[existing_data.index < cutoff_date]
                    
                    # Fetch new data
                    ticker_obj = yf.Ticker(ticker)
                    new_data = ticker_obj.history(start=cutoff_date.strftime('%Y-%m-%d'))
                    new_data.index = new_data.index.tz_localize(None)
                    
                    # Combine datasets
                    combined_data = pd.concat([existing_data, new_data])
                    combined_data = combined_data[~combined_data.index.duplicated(keep='last')]
                    combined_data.sort_index(inplace=True)
                    
                    # Update database
                    success = self.store_dataframe(combined_data, table_name)
                    if not success:
                        raise ValueError(f"Failed to update data for {ticker}")
                    
                    return combined_data[(combined_data.index >= start_date) & (combined_data.index <= end_date)]
                
                # If data is current enough, return filtered data from database
                df = pd.read_sql_table(table_name, self.engine)
                df.set_index('Date', inplace=True)
                return df[(df.index >= start_date) & (df.index <= end_date)]
            
            # If table doesn't exist, check if beyond default period
            default_start = (pd.Timestamp.now() - pd.DateOffset(years=20)).strftime('%Y-%m-%d')
            if start_date < default_start:
                logging.info(f"Requested start date {start_date} is beyond default period and no existing data")
                # Fetch data directly from yfinance for the specific period
                ticker_obj = yf.Ticker(ticker)
                df = ticker_obj.history(start=start_date, end=end_date)
                if df.empty:
                    raise ValueError(f"No data found for {ticker} in requested date range")
                df.index = df.index.tz_localize(None)
                return df
            
            # If within default period, store all historical data first
            logging.info(f"Data not found in database for {ticker}, fetching data")
            success = self.store_historical_data(ticker)
            if not success:
                raise ValueError(f"Failed to store data for {ticker}")
            df = pd.read_sql_table(table_name, self.engine)
            df.set_index('Date', inplace=True)
            return df[(df.index >= start_date) & (df.index <= end_date)]
                    
        except Exception as e:
            logging.error(f"Error in get_historical_data for {ticker}: {str(e)}")
            raise
        
        
    def get_financial_data(self, ticker: str, metric_description: str, 
                        start_year: str, end_year: str) -> pd.Series:
        """
        Get financial data from MySQL database or ROIC API if not exists/incomplete.
        """
        cleaned_ticker = self.clean_ticker_for_table_name(ticker)
        table_name = f"roic_{cleaned_ticker}"
        MAX_MISSING_YEARS_TOLERANCE = 2 
        # company_name = yf.Ticker(ticker).info['longName']
        
        try:
            # First try to get data from database
            # if "^" in ticker or "-" in ticker or "=" in ticker:
            #     return None
            if not is_stock(ticker):
                return None
            # if company_name:
            # # Check for excluded terms using regex (case insensitive)
            #     excluded_terms = r'shares|etf|index|trust'
            #     if re.search(excluded_terms, company_name, re.IGNORECASE):
            #         return None
                
            
            if self.table_exists(table_name):
                print(f"Getting financial data for {ticker} from database")
                df = pd.read_sql_table(table_name, self.engine)
                
                metric_field = self.METRICS.get(metric_description.lower())
                if metric_field in df.columns:
                    df['fiscal_year'] = df['fiscal_year'].astype(int)
                    
                    # Filter for requested years
                    mask = (df['fiscal_year'] >= int(start_year)) & (df['fiscal_year'] <= int(end_year))
                    filtered_df = df[mask]
                    
                    # Check if we have all the years we need
                    requested_years = set(range(int(start_year), int(end_year) + 1))
                    actual_years = set(filtered_df['fiscal_year'].values)
                    missing_years = requested_years - actual_years
                    
                    return pd.Series(
                            filtered_df[metric_field].values,
                            index=filtered_df['fiscal_year'],
                            name=metric_description
                        )
                    # if len(missing_years) == 0:
                    # if len(missing_years) <= MAX_MISSING_YEARS_TOLERANCE:
                    #     return pd.Series(
                    #         filtered_df[metric_field].values,
                    #         index=filtered_df['fiscal_year'],
                    #         name=metric_description
                    #     )
                    # else:
                    #     print(f"Incomplete data for {ticker}, fetching from API")
                    #     # If data is incomplete, fetch all data and update database
                    #     success = self.store_financial_data(ticker, start_year, end_year)
                    #     if success:
                    #         df = pd.read_sql_table(table_name, self.engine)
                    #         df['fiscal_year'] = df['fiscal_year'].astype(int)
                    #         mask = (df['fiscal_year'] >= int(start_year)) & (df['fiscal_year'] <= int(end_year))
                    #         filtered_df = df[mask]
                    #         return pd.Series(
                    #             filtered_df[metric_field].values,
                    #             index=filtered_df['fiscal_year'],
                    #             name=metric_description
                    #         )

            # If not in database, store it first
            print(f"Data not found in database for {ticker}, fetching from API")
            success = self.store_financial_data(ticker, start_year, end_year)
            if success:
                df = pd.read_sql_table(table_name, self.engine)
                metric_field = self.METRICS.get(metric_description.lower())
                df['fiscal_year'] = df['fiscal_year'].astype(int)
                mask = (df['fiscal_year'] >= int(start_year)) & (df['fiscal_year'] <= int(end_year))
                filtered_df = df[mask]
                return pd.Series(
                    filtered_df[metric_field].values,
                    index=filtered_df['fiscal_year'],
                    name=metric_description
                )
            else:
                return None
                
        except Exception as e:
            print(f"Error in get_financial_data for {ticker}: {str(e)}")
            return None
    
    def store_historical_data(self, ticker: str, start_date: str = None, end_date: str = None) -> bool:
        """
        Fetch and store historical price data from yfinance.
        Uses custom date range if provided, otherwise defaults to 10 years of data.
        
        Parameters:
        -----------
        ticker : str
            Stock ticker symbol
        start_date : str, optional
            Start date in YYYY-MM-DD format
        end_date : str, optional
            End date in YYYY-MM-DD format
        
        Returns:
        --------
        bool
            Success status of the operation
        """
        try:
            print(f"Fetching historical data for {ticker} from yfinance")
            ticker_obj = yf.Ticker(ticker)
            
            # Get the latest trading day (last Friday if weekend)
            latest_trading_day = pd.Timestamp.now()
            while latest_trading_day.weekday() > 4:  # 5 = Saturday, 6 = Sunday
                latest_trading_day -= pd.Timedelta(days=1)
                
            # If no dates specified, use default 10 year range
            if start_date is None or end_date is None:
                end_date = latest_trading_day.strftime('%Y-%m-%d')
                start_date = (latest_trading_day - pd.DateOffset(years=20)).strftime('%Y-%m-%d')
                df = ticker_obj.history(start=start_date)
            else:
                # Use specified date range but ensure end_date isn't beyond latest trading day
                end_date = min(pd.to_datetime(end_date), latest_trading_day).strftime('%Y-%m-%d')
                df = ticker_obj.history(start=start_date, end=end_date)
            
            if df.empty:
                print(f"No historical data found for {ticker}")
                return False
            
            # Process the data
            df.index = df.index.tz_localize(None)
            cleaned_ticker = self.clean_ticker_for_table_name(ticker)
            table_name = f"his_{cleaned_ticker}"
            
            # Store in database
            return self.store_dataframe(df, table_name)
                    
        except Exception as e:
            print(f"Error storing historical data for {ticker}: {e}")
            return False
    
    
    # In data_service.py

    def store_financial_data(self, ticker: str, start_year: str = None, end_year: str = None) -> bool:
        """Fetch and store financial data from ROIC API"""
        try:
            print(f"Fetching financial data for {ticker} from ROIC API")
            
            # If no years specified, use last 10 years
            if not start_year or not end_year:
                current_year = datetime.now().year
                end_year = str(current_year)
                start_year = str(current_year - 10)

            all_metrics_data = []
            
            # Fetch data for each metric
            for metric_description in self.METRICS:
                try:
                    metric_field = self.METRICS[metric_description]
                    query = f"get({metric_field}(fa_period_reference=range('{start_year}', '{end_year}'))) for('{ticker}')"
                    url = f"{self.BASE_URL}?query={query}&apikey={self.API_KEY}"

                    # Add delay between requests
                    sleep(1.5)  # 1.5 seconds between requests
                    
                    response = requests.get(url)
                    
                    # Handle rate limiting
                    if response.status_code == 429:  # Too Many Requests
                        print(f"Rate limit hit for {ticker}, waiting 60 seconds...")
                        sleep(60)  # Wait 60 seconds if rate limited
                        response = requests.get(url)  # Try again
                    
                    response.raise_for_status()
                    
                    df = pd.DataFrame(response.json())
                    if not df.empty:
                        df.columns = df.iloc[0]
                        df = df.drop(0).reset_index(drop=True)
                        all_metrics_data.append(df)
                    
                except Exception as metric_error:
                    print(f"Error fetching {metric_description} for {ticker}: {str(metric_error)}")
                    continue  # Continue with next metric if one fails

            if not all_metrics_data:
                print(f"No financial data found for {ticker}")
                return False

            # Combine all metrics data
            try:
                combined_df = pd.concat(all_metrics_data, axis=1)
                combined_df = combined_df.loc[:,~combined_df.columns.duplicated()]
                
                # Store in database
                cleaned_ticker = self.clean_ticker_for_table_name(ticker)
                table_name = f"roic_{cleaned_ticker}"
                
                # Convert DataFrame columns that should be numeric
                numeric_columns = ['fiscal_year']  # Add other numeric columns as needed
                for col in numeric_columns:
                    if col in combined_df.columns:
                        combined_df[col] = pd.to_numeric(combined_df[col], errors='coerce')
                
                return self.store_dataframe(combined_df, table_name)
                
            except Exception as combine_error:
                print(f"Error combining data for {ticker}: {str(combine_error)}")
                return False
                    
        except Exception as e:
            print(f"Error storing financial data for {ticker}: {e}")
            return False
            
    def get_analysis_dates(self, end_date: str, lookback_type: str, 
                            lookback_value: int) -> str:
            """
            Calculate start date based on lookback period

            Parameters:
            -----------
            end_date : str
                End date in YYYY-MM-DD format
            lookback_type : str
                Type of lookback period ('quarters' or 'days')
            lookback_value : int
                Number of quarters or days to look back

            Returns:
            --------
            str
                Start date in YYYY-MM-DD format
            """
            try:
                # Handle None or empty end_date
                if not end_date:
                    end_date = datetime.now().strftime("%Y-%m-%d")
                    
                # Validate date format
                try:
                    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                except ValueError:
                    print(f"Invalid date format: {end_date}, using current date")
                    end_dt = datetime.now()
                    
                if lookback_type == 'quarters':
                    start_dt = end_dt - relativedelta(months=3*lookback_value)
                else:  # days
                    start_dt = end_dt - relativedelta(days=lookback_value)
                    
                return start_dt.strftime("%Y-%m-%d")
                
            except Exception as e:
                print(f"Error calculating analysis dates: {str(e)}")
                raise

    def create_metrics_table(self, ticker: str, metrics: list, 
                           start_year: str, end_year: str) -> pd.DataFrame:
        """
        Creates a combined table of all metrics with selective growth rates.
        If no data is available, returns None without showing table header.

        Parameters:
        -----------
        ticker : str
            Stock ticker symbol
        metrics : list
            List of metrics to fetch
        start_year : str
            Start year in YYYY format
        end_year : str
            End year in YYYY format

        Returns:
        --------
        pd.DataFrame or None
            DataFrame containing metrics and growth rates or None if no data available
        """
        data = {}
        growth_rates = {}

        # Check if any metrics have data before creating table
        has_data = False
        for metric in metrics:
            metric = metric.lower()
            series = self.get_financial_data(ticker.upper(), metric, start_year, end_year)
            
            if series is not None:
                has_data = True
                data[metric] = series

                # Calculate CAGR only for specified metrics
                if metric in self.CAGR_METRICS:
                    try:
                        first_value = series.iloc[0]
                        last_value = series.iloc[-1]
                        num_years = len(series) - 1
                        if num_years > 0 and first_value > 0 and last_value > 0:
                            growth_rate = ((last_value / first_value) ** (1 / num_years) - 1) * 100
                            growth_rates[metric] = growth_rate
                    except Exception as e:
                        print(f"Error calculating CAGR for {metric}: {str(e)}")
                        growth_rates[metric] = None

        # If no data was found for any metrics, return None without creating table
        if not has_data:
            return None

        try:
            # Create main DataFrame with metrics
            df = pd.DataFrame(data).T

            # Add growth rates column only for specified metrics
            df['CAGR %'] = None  # Initialize with None
            for metric in self.CAGR_METRICS:
                if metric in growth_rates and metric in df.index:
                    df.at[metric, 'CAGR %'] = growth_rates[metric]

            return df
        except Exception as e:
            print(f"Error creating metrics table: {str(e)}")
            return None

    def calculate_returns(self, df: pd.DataFrame) -> pd.Series:
        """
        Calculate daily returns for a price series

        Parameters:
        -----------
        df : pd.DataFrame
            DataFrame containing price data

        Returns:
        --------
        pd.Series
            Series containing daily returns
        """
        try:
            if 'Close' not in df.columns:
                raise ValueError("Price data must contain 'Close' column")
                
            returns = df['Close'].pct_change()
            returns.fillna(0, inplace=True)
            return returns
            
        except Exception as e:
            print(f"Error calculating returns: {str(e)}")
            raise

    @retry_with_backoff(retries=3)
    def store_historical_data_with_retry(self, ticker, start_date, end_date):
        rate_limiter = RateLimiter(calls_per_second=2)
        rate_limiter.wait()
        return self.store_historical_data(ticker, start_date, end_date)
    @retry_with_backoff(retries=3)
    def store_financial_data_with_retry(self, ticker: str, start_year: str = None, end_year: str = None) -> bool:
        """Store financial data with retry mechanism"""
        rate_limiter = RateLimiter(calls_per_second=1)  # Limit to 1 call per second
        rate_limiter.wait()
        return self.store_financial_data(ticker, start_year, end_year)
