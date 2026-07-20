#!/usr/bin/env python3

"""

Streamlit dashboard for Rogers HVAC RUL predictions.

Reads sites_data.json and displays 1020+ sites with Mode 3 rolling median RUL.

"""



import streamlit as st

import pandas as pd

import json

import os

from datetime import datetime

import plotly.graph_objects as go

import numpy as np                     



# ============================================================================

# PAGE CONFIG

# ============================================================================

                                        

st.set_page_config(

    page_title="Rogers HVAC RUL Dashboard",

    page_icon="📊",

    layout="wide",

    initial_sidebar_state="expanded"   

)



st.title("🔧 Rogers HVAC Filter RUL Prediction Dashboard")

st.markdown("**Mode 3: Rolling Median + Linear Trend Projection** | Multi-Site Unified View")



# ============================================================================

# LOAD DATA

# ============================================================================

                                        

@st.cache_data

def load_data():

    """Load sites_data.json if it exists."""

    if not os.path.exists('sites_data.json'):

        return None, "sites_data.json not found. Run query_sites.py first."



    try:

        with open('sites_data.json', 'r') as f:

            data = json.load(f)

        return data, None

    except Exception as e:

        return None, f"Error loading sites_data.json: {e}"



data, error = load_data()



if error:

    st.error(f"❌ {error}")            

    st.stop()



# ============================================================================

# TOP-LEVEL METRICS

# ============================================================================



col1, col2, col3, col4, col5 = st.columns(5)



with col1:

    st.metric("Total Sites", data['sites_total'])



with col2:

    st.metric("Queried Successfully", data['sites_queried'],

            f"{data['success_rate']:.1f}%")



with col3:

    st.metric("Failed", data['sites_failed'])



with col4:                             

    query_time = data['query_elapsed_seconds']

    minutes = int(query_time // 60)

    st.metric("Query Time", f"{minutes}m {int(query_time % 60)}s")



with col5:

    query_dt = datetime.fromisoformat(data['query_timestamp'])

    st.metric("Last Update", query_dt.strftime("%Y-%m-%d %H:%M"))



# ============================================================================

# URGENCY SUMMARY (SUCCESSFUL SITES)

# ============================================================================



st.subheader("📊 Urgency Summary (Successful Sites)")



urgency = data['urgency_summary']

col1, col2, col3 = st.columns(3)



with col1:

    st.metric("🔴 URGENT (<14d)", urgency.get('URGENT', 0),

            delta=f"{100*urgency.get('URGENT', 0)/max(1, data['sites_queried']):.1f}%")



with col2:

    st.metric("🟡 WARNING (14-30d)", urgency.get('WARNING', 0),

            delta=f"{100*urgency.get('WARNING', 0)/max(1, data['sites_queried']):.1f}%")



with col3:

    st.metric("🟢 OK (≥30d)", urgency.get('OK', 0),

            delta=f"{100*urgency.get('OK', 0)/max(1, data['sites_queried']):.1f}%")



# ============================================================================

# ERROR BREAKDOWN

# ============================================================================



if data['sites_failed'] > 0:

    st.subheader("⚠️  Failure Breakdown")



    error_breakdown = data.get('error_breakdown', {})



    col1, col2 = st.columns(2)



    with col1:                         

        # Bar chart of errors

        if error_breakdown:

            errors_df = pd.DataFrame([

                {'Error Type': k, 'Count': v}

                for k, v in sorted(error_breakdown.items(), key=lambda x: -x[1])

            ])



            fig = go.Figure(data=[

                go.Bar(x=errors_df['Error Type'], y=errors_df['Count'], marker_color='indianred')

            ])

            fig.update_layout(

                title="Failures by Type",

                xaxis_title="Error Type",

                yaxis_title="Count",

                height=300,

                showlegend=False

            )

            st.plotly_chart(fig, use_container_width=True)



    with col2:

        # List of error counts         

        st.write("**Error Counts:**")

        for error_type, count in sorted(error_breakdown.items(), key=lambda x: -x[1]):

            pct = 100 * count / data['sites_failed']

            st.write(f"  • `{error_type}`: {count} ({pct:.1f}%)")



# ============================================================================

# UNREACHABLE IP RANGES

# ============================================================================



unreachable = data.get('unreachable_ip_ranges', {})

if unreachable:

    st.subheader("🌐 Unreachable IP Ranges (Network Timeout)")



    for ip_range, sites_list in sorted(unreachable.items()):

        num_sites = len(sites_list)

        with st.expander(f"**{ip_range}.x.x** — {num_sites} unreachable sites"):

            st.write(", ".join(sites_list[:10]) +

                    (f", ... +{num_sites-10} more" if num_sites > 10 else ""))



# ============================================================================

# SITES TABLE (SUCCESSFUL ONLY)

# ============================================================================



st.subheader("📋 Sites Table (Sorted by Urgency)")



sites_data = data['sites']

successful_sites = [s for s in sites_data.values() if s.get('success')]



if successful_sites:

    # Convert to DataFrame

    sites_df = pd.DataFrame([

        {                              

            'Site ID': s['site_id'],

            'Site Name': s['site_name'],

            'IP': s['ip'],             

            'Urgency': s.get('urgency', '?'),

            'RUL (days)': s.get('rul_days'),

            'R²': s.get('r2'),

            'Episodes': s.get('episodes_count'),

            'Current ΔT': s.get('current_dt'),

            'Baseline ΔT': s.get('baseline_dt'),

            '% Life Used': s.get('pct_life'),

        }

        for s in successful_sites

    ])



    # Sort by RUL (urgent first)

    sites_df = sites_df.sort_values('RUL (days)', na_position='last')



    # Sidebar filters

    st.sidebar.subheader("🔍 Filters")



    urgency_filter = st.sidebar.multiselect(

        "Filter by Urgency:",

        options=['URGENT', 'WARNING', 'OK'],

        default=['URGENT', 'WARNING', 'OK']

    )



    search_term = st.sidebar.text_input("Search by Site ID or Name:")



    # Apply filters

    filtered_df = sites_df[sites_df['Urgency'].isin(urgency_filter)]



    if search_term:

        filtered_df = filtered_df[

            filtered_df['Site ID'].str.contains(search_term, case=False, na=False) |

            filtered_df['Site Name'].str.contains(search_term, case=False, na=False)

        ]



    # Display table

    st.dataframe(                      

        filtered_df.style.format({

            'RUL (days)': '{:.1f}',

            'R²': '{:.3f}',

            'Current ΔT': '{:.1f}',

            'Baseline ΔT': '{:.1f}',

            '% Life Used': '{:.1f}',

        }),

        use_container_width=True

    )



    # Per-site details

    st.subheader("📈 Site Details")



    selected_site_id = st.selectbox(

        "Select a site to view details:",

        options=sorted([s['site_id'] for s in successful_sites]),

        format_func=lambda x: f"{x} — {[s['site_name'] for s in successful_sites if s['site_id'] == x][0]}"

    )



    if selected_site_id:

        site_detail = sites_data[selected_site_id]



        col1, col2, col3, col4 = st.columns(4)



        with col1:

            st.metric("Site ID", site_detail['site_id'])

        with col2:                     

            st.metric("Urgency", site_detail.get('urgency', '?'))

        with col3:

            st.metric("RUL", f"{site_detail.get('rul_days', 0):.1f}d")

        with col4:

            st.metric("R²", f"{site_detail.get('r2', 0):.3f}")



        col1, col2, col3 = st.columns(3)



        with col1:

            st.metric("Episodes", site_detail.get('episodes_count', 0))

        with col2:

            st.metric("Current ΔT", f"{site_detail.get('current_dt', 0):.1f}°C")

        with col3:

            st.metric("Baseline ΔT", f"{site_detail.get('baseline_dt', 0):.1f}°C")



        # Rolling median trend plot

        if 'rolling_median' in site_detail and 'onset_deltas' in site_detail:

            fig = go.Figure()



            episodes = list(range(len(site_detail['onset_deltas'])))



            # Raw onset ΔT

            fig.add_trace(go.Scatter(

                x=episodes,

                y=site_detail['onset_deltas'],

                mode='markers',

                name='Onset ΔT (raw)',

                marker=dict(size=6, color='lightblue')

            ))



            # Rolling median           

            fig.add_trace(go.Scatter(

                x=episodes,

                y=site_detail['rolling_median'],

                mode='lines',

                name='Rolling Median',

                line=dict(color='blue', width=2)

            ))



            # Linear fit

            slope = site_detail.get('slope', 0)

            intercept = site_detail['rolling_median'][0] - slope * 0

            fit_line = [intercept + slope * ep for ep in episodes]

            fig.add_trace(go.Scatter(

                x=episodes,

                y=fit_line,

                mode='lines',

                name='Linear Trend',

                line=dict(color='red', width=2, dash='dash')

            ))



            # Failure threshold        

            failure_dt = site_detail.get('failure_dt', 10.0)

            fig.add_hline(

                y=failure_dt,

                line_dash="dot",

                line_color="red",

                annotation_text=f"Failure ΔT ({failure_dt:.1f}°C)",

                annotation_position="right"

            )



            fig.update_layout(

                title=f"RUL Trend — {site_detail['site_id']}",

                xaxis_title="Episode Number",

                yaxis_title="ΔT (°C)",

                height=400,

                hovermode='x unified'

            )                          

            st.plotly_chart(fig, use_container_width=True)



        # Statistics                   

        st.write("**RUL Calculation Summary:**")

        st.json({

            'slope': round(site_detail.get('slope', 0), 4),

            'r2': round(site_detail.get('r2', 0), 3),

            'episodes_count': site_detail.get('episodes_count', 0),

            'baseline_dt': round(site_detail.get('baseline_dt', 0), 1),

            'current_dt': round(site_detail.get('current_dt', 0), 1),

            'failure_dt': round(site_detail.get('failure_dt', 0), 1),

            'rul_days': round(site_detail.get('rul_days', 0), 1),

            'pct_life': round(site_detail.get('pct_life', 0), 1),

            'last_episode_time': site_detail.get('last_episode_time', 'N/A'),

        })



else:

    st.warning("⚠️  No successful sites to display. Check query results.")



# ============================================================================

# FOOTER                               

# ============================================================================



st.markdown("---")

st.markdown("""

**How to interpret:**

- 🔴 **URGENT**: RUL < 14 days → Replace filter immediately

- 🟡 **WARNING**: 14-30 days → Plan replacement soon

- 🟢 **OK**: ≥ 30 days → Monitor regularly

- **R²**: Trend confidence (≥0.25 is reliable)

- **ΔT**: Temperature rise across filter (indicates clogging)

""")

