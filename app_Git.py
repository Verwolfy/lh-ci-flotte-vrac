"""
═══════════════════════════════════════════════════════════════════════════════
APPLICATION INTÉGRALE — TMS & BUSINESS INTELLIGENCE VRAC (Version Multi-jours)
LH Côte d'Ivoire | Mapping Vrac & Planification Flotte
═══════════════════════════════════════════════════════════════════════════════
"""

import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import folium
from streamlit_folium import st_folium
import plotly.express as px
import os
import math
import datetime

# ── CONFIGURATION DE LA PAGE ─────────────────────────────────────────────────
st.set_page_config(page_title="LH CI - Flotte Vrac & Dispatch", page_icon="📅", layout="wide")

# ── PARAMÈTRES & CHEMINS SÉCURISÉS ───────────────────────────────────────────
SHEET_ID = "14ix_pet8yn7b_yG7rfd7GwqcsNFEU4BSCVzWr8FVRBc"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "service_account.json")

def fetch_with_auto_head(ws):
    """Télécharge la grille brute et trouve automatiquement la ligne d'en-tête technique"""
    try:
        raw_data = ws.get_all_values()
        if not raw_data:
            return pd.DataFrame()
            
        for i, row in enumerate(raw_data):
            cleaned_row = [str(cell).strip() for cell in row]
            if "client_id" in cleaned_row or "parametre" in cleaned_row or "camion_id" in cleaned_row or "id_commande" in cleaned_row:
                headers = cleaned_row
                data = raw_data[i + 1:]
                return pd.DataFrame(data, columns=headers)
                
        return pd.DataFrame() 
    except Exception as e:
        st.error(f"Erreur de lecture de l'onglet {ws.title}: {e}")
        return pd.DataFrame()

# ── CHARGEMENT ET TRAITEMENT DES DONNÉES ─────────────────────────────────────
@st.cache_data(ttl=600)
def load_data():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    # --- AUTHENTIFICATION SÉCURISÉE CLOUD & LOCAL ---
    if "gcp_service_account" in st.secrets:
        try:
            creds_dict = dict(st.secrets["gcp_service_account"])
            if "private_key" in creds_dict:
                creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        except Exception as e:
            st.error(f"Erreur lors de la lecture des Secrets Cloud : {e}")
            return pd.DataFrame(), pd.DataFrame(), 4500.0, 27.0, 0.92, 5.3599, -4.0083
    else:
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
        else:
            st.error("Fichier 'service_account.json' introuvable en local et aucun Secret détecté sur le Cloud.")
            return pd.DataFrame(), pd.DataFrame(), 4500.0, 27.0, 0.92, 5.3599, -4.0083
    
    try:
        gc = gspread.authorize(creds)
        wb = gc.open_by_key(SHEET_ID)
    except Exception as e:
        st.error(f"Erreur de connexion à Google Sheets : {e}")
        return pd.DataFrame(), pd.DataFrame(), 4500.0, 27.0, 0.92, 5.3599, -4.0083
    
    # Lecture des onglets nécessaires
    df_sites = fetch_with_auto_head(wb.worksheet("CLIENTS_SITES"))
    df_flotte = fetch_with_auto_head(wb.worksheet("FLOTTE_CLIENTS"))
    df_volumes = fetch_with_auto_head(wb.worksheet("VOLUMES_SAP"))
    df_flotte_lh = fetch_with_auto_head(wb.worksheet("FLOTTE_LH"))
    
    # Extraction des variables globales depuis PARAMETRES
    try:
        df_params = fetch_with_auto_head(wb.worksheet("PARAMETRES")).set_index("parametre")
        spot_ref = float(df_params.loc["tarif_spot_ref_fcfa_t", "valeur"])
    except: spot_ref = 4500.0
        
    try: tonnage_std = float(df_params.loc["tonnage_camion_std", "valeur"])
    except: tonnage_std = 27.0
        
    try: coeff_remplissage = float(df_params.loc["coef_remplissage_min", "valeur"])
    except: coeff_remplissage = 0.92

    try:
        usine_lat = float(df_params.loc["usine_latitude", "valeur"])
        usine_lon = float(df_params.loc["usine_longitude", "valeur"])
    except:
        usine_lat = 5.3599
        usine_lon = -4.0083

    # Sécurité si un DataFrame vital est vide
    for df_temp, name in [(df_sites, "CLIENTS_SITES"), (df_flotte, "FLOTTE_CLIENTS"), (df_volumes, "VOLUMES_SAP")]:
        if df_temp.empty or "client_id" not in df_temp.columns:
            st.error(f"⚠️ Impossible de trouver la colonne 'client_id' dans l'onglet : {name}.")
            return pd.DataFrame(), pd.DataFrame(), spot_ref, tonnage_std, coeff_remplissage, usine_lat, usine_lon

    # Nettoyage des chaînes
    for d in [df_sites, df_flotte, df_volumes, df_flotte_lh]:
        if "client_id" in d.columns: 
            d["client_id"] = d["client_id"].astype(str).str.strip()
    
    if "cap_mensuelle_t" in df_flotte.columns:
        df_flotte.rename(columns={"cap_mensuelle_t": "capacite_mensuelle_t"}, inplace=True)
    
    # Conversions numériques
    cap_col = "capacite_mensuelle_t" if "capacite_mensuelle_t" in df_flotte.columns else df_flotte.columns[3]
    df_flotte[cap_col] = pd.to_numeric(df_flotte[cap_col], errors="coerce").fillna(0)
    
    vol_col = "volume_exw_t" if "volume_exw_t" in df_volumes.columns else df_volumes.columns[5]
    df_volumes[vol_col] = pd.to_numeric(df_volumes[vol_col], errors="coerce").fillna(0)
    
    # Agrégations pour le surplus
    agg_flotte = df_flotte.groupby("client_id").agg(capacite_t_mois=(cap_col, "sum")).reset_index()
    agg_volumes = df_volumes.groupby("client_id").agg(besoin_exw_t_mois=(vol_col, "mean")).reset_index()
    
    # Jointure principale
    df_main = df_sites.merge(agg_flotte, on="client_id", how="left").merge(agg_volumes, on="client_id", how="left")
    df_main.fillna({"capacite_t_mois": 0, "besoin_exw_t_mois": 0}, inplace=True)
    df_main["surplus_t_mois"] = (df_main["capacite_t_mois"] - df_main["besoin_exw_t_mois"]).clip(lower=0)
    
    # Calculs Financiers
    df_main["tarif_spot_ref"] = spot_ref
    df_main["tarif_cible_client"] = df_main["tarif_spot_ref"] * 0.85
    df_main["gain_par_tonne"] = df_main["tarif_spot_ref"] - df_main["tarif_cible_client"]
    df_main["economie_potentielle_mensuelle"] = df_main["surplus_t_mois"] * df_main["gain_par_tonne"]
    
    return df_main, df_flotte_lh, spot_ref, tonnage_std, coeff_remplissage, usine_lat, usine_lon

# ── INTERFACE UTILISATEUR (STREAMLIT) ────────────────────────────────────────
st.title("🚛 LH Côte d'Ivoire — Pilotage Logistique & Financier Vrac")

if st.sidebar.button("🔄 Rafraîchir les données"):
    st.cache_data.clear()
    st.rerun()

try:
    df, df_lh, spot_ref, tonnage_std, coeff_remplissage, usine_lat, usine_lon = load_data()
    
    if not df.empty and "client_nom" in df.columns:
        surplus_total = df['surplus_t_mois'].sum()
        economie_totale = df['economie_potentielle_mensuelle'].sum()
        
        # Section haute : KPIs Généraux de Synthèse
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Capacité Totale Flotte Clients", f"{df['capacite_t_mois'].sum():,.0f} T/mois")
        col2.metric("Surplus Total Exploitable", f"{surplus_total:,.0f} T/mois", delta=f"{surplus_total/tonnage_std:.0f} voyages théoriques", delta_color="inverse")
        col3.metric("Économie Mensuelle Target", f"{economie_totale:,.0f} FCFA", delta=f"{economie_totale * 12:,.0f} FCFA / an")
        
        if not df_lh.empty and "status" in df_lh.columns:
            nb_camions_actifs = len(df_lh[df_lh["status"].str.upper() == "ACTIF"])
            col4.metric("Camions Actifs Pool LH", nb_camions_actifs)
        else:
            col4.metric("Camions Actifs Pool LH", "Dispo (Sheet)")

        st.markdown("---")
        
        # Structure en 4 Onglets Métiers
        tab1, tab2, tab3, tab4 = st.tabs([
            "🗺️ Carte Interactive", 
            "📊 Analyse Surplus de Flotte", 
            "💸 Rentabilité Financière",
            "📅 Planification des Tournées (Dispatch)"
        ])
        
        # ONGLET 1 : CARTE INTERACTIVE
        with tab1:
            st.subheader("Cartographie des sites clients")
            df_map = df.dropna(subset=['latitude', 'longitude'])
            df_map = df_map[(df_map['latitude'] != '') & (df_map['longitude'] != '')]
            if not df_map.empty:
                m = folium.Map(location=[usine_lat, usine_lon], zoom_start=11, tiles="OpenStreetMap")
                folium.Marker([usine_lat, usine_lon], popup="Usine LH CI", icon=folium.Icon(color="black", icon="industry", prefix='fa')).add_to(m)
                for idx, row in df_map.iterrows():
                    try:
                        lat, lon = float(row['latitude']), float(row['longitude'])
                        color = "green" if row['surplus_t_mois'] > 0 else "blue"
                        popup_text = f"<b>{row['client_nom']}</b><br>Surplus: {row['surplus_t_mois']} T/mois<br>Gain Estimé: {row['economie_potentielle_mensuelle']:,.0f} FCFA"
                        folium.Marker([lat, lon], popup=popup_text, icon=folium.Icon(color=color)).add_to(m)
                    except: pass
                st_folium(m, width=1000, height=450)
            else:
                st.warning("Aucune coordonnée GPS valide trouvée.")

        # ONGLET 2 : ANALYSE DES SURPLUS
        with tab2:
            st.subheader("Volume de surplus disponible")
            df_chart = df[df['surplus_t_mois'] > 0].sort_values("surplus_t_mois", ascending=False).head(10)
            if not df_chart.empty:
                fig = px.bar(df_chart, x="client_nom", y="surplus_t_mois", 
                             title="Top 10 Clients avec le plus grand Surplus de Flotte",
                             labels={"client_nom": "Client", "surplus_t_mois": "Surplus (T/mois)"},
                             color="surplus_t_mois", color_continuous_scale="Viridis")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Aucun surplus volumique détecté pour le moment.")
                
            st.dataframe(df[["client_id", "client_nom", "zone", "capacite_t_mois", "besoin_exw_t_mois", "surplus_t_mois"]].sort_values("surplus_t_mois", ascending=False), use_container_width=True)

        # ONGLET 3 : ANALYSE FINANCIÈRE DE L'OPPORTUNITÉ
        with tab3:
            st.subheader("Matrice des opportunités de gains")
            df_finance_chart = df[df['economie_potentielle_mensuelle'] > 0].sort_values("economie_potentielle_mensuelle", ascending=False).head(10)
            
            if not df_finance_chart.empty:
                fig_fin = px.pie(df_finance_chart, values='economie_potentielle_mensuelle', names='client_nom',
                                 title='Répartition des économies potentielles par compte client (Top 10)')
                st.plotly_chart(fig_fin, use_container_width=True)
                
            df_display_fin = df[["client_id", "client_nom", "zone", "surplus_t_mois", "tarif_cible_client", "economie_potentielle_mensuelle"]].copy()
            df_display_fin.columns = ["Code SAP", "Nom Client", "Zone", "Surplus (T/mois)", "Tarif Cible (FCFA/T)", "Économie Potentielle (FCFA/mois)"]
            st.dataframe(df_display_fin.sort_values("Économie Potentielle (FCFA/mois)", ascending=False), use_container_width=True)

        # ONGLET 4 : MOTEUR DE PLANIFICATION MULTI-JOURS & MULTI-SITES INTERACTIF
        with tab4:
            st.subheader("📅 Planification Multi-jours & Gestion Multi-sites")
            st.write("Programmez vos ordres de transport. Sélectionnez la combinaison exacte du Client et de son Site de livraison.")

            # Extraction dynamique des options de choix pour sécuriser la saisie
            df_combinaison = df.copy()
            nom_colonne_site = "site" if "site" in df_combinaison.columns else "zone"
            
            # Génération du libellé unique combiné pour la liste déroulante
            df_combinaison["destination_complete"] = (
                df_combinaison["client_nom"] + " — " + df_combinaison[nom_colonne_site] + " (" + df_combinaison["zone"] + ")"
            )
            
            liste_destinations_choix = sorted(df_combinaison["destination_complete"].dropna().unique().tolist())

            if "df_saisie_multi" not in st.session_state:
                st.session_state.df_saisie_multi = pd.DataFrame(
                    [
                        {
                            "date_livraison": datetime.date.today(), 
                            "destination": liste_destinations_choix[0] if liste_destinations_choix else "", 
                            "volume_t": 54
                        }
                    ]
                )

            st.markdown("### ✍️ Grille des Commandes Planifiées (Sélection par Site)")
            st.info("💡 **Mode d'emploi :** Cliquez sur '+' pour planifier un voyage. Sélectionnez le couple Client — Site dans la liste. Le système détectera automatiquement la zone et générera le code commande.")
            
            commandes_editees = st.data_editor(
                st.session_state.df_saisie_multi,
                num_rows="dynamic",
                column_config={
                    "date_livraison": st.column_config.DateColumn("Date de Livraison", required=True, format="DD/MM/YYYY"),
                    "destination": st.column_config.SelectboxColumn("Destination (Client — Site Destination)", options=liste_destinations_choix, required=True),
                    "volume_t": st.column_config.NumberColumn("Volume (Tonnes)", min_value=0, step=27, required=True),
                },
                use_container_width=True,
                key="editeur_multi_jours"
            )

            st.markdown("---")
            if st.button("⚡ Valider, Générer les Codes & Sauvegarder le Planning"):
                df_cmd = pd.DataFrame(commandes_editees)
                df_cmd = df_cmd[df_cmd["destination"] != ""]  # Nettoyage des lignes blanches

                if not df_cmd.empty:
                    st.markdown("### 📋 Plan d'Affectation Transport Optimisé (Par Site)")
                    
                    df_surplus_dispo = df_combinaison[df_combinaison["client_id"] != ""].copy()
                    suggestions = []
                    mises_a_jour_sheet = [["date_livraison", "id_commande", "client_nom", "site", "zone", "volume_t", "camion_recommande", "economie_fcfa"]]
                    
                    sequence_compteur = 1
                    
                    for idx, row_cmd in df_cmd.iterrows():
                        dest_selectionnee = row_cmd["destination"]
                        vol_cmd = row_cmd["volume_t"]
                        
                        # Conversion de sécurité de la date
                        date_obj = row_cmd["date_livraison"]
                        if isinstance(date_obj, str):
                            date_str_code = date_obj.replace("-", "")
                            date_str_sheet = date_obj
                        else:
                            date_str_code = date_obj.strftime("%Y%m%d")
                            date_str_sheet = date_obj.strftime("%Y-%m-%d")
                        
                        # Génération automatique du numéro d'ordre de transport
                        code_auto = f"CMD-{date_str_code}-{sequence_compteur:03d}"
                        sequence_compteur += 1
                        
                        nb_voyages = math.ceil(vol_cmd / tonnage_std)
                        
                        # EXTRACTION DU CLIENT, DU SITE ET DE LA ZONE
                        match_base = df_surplus_dispo[df_surplus_dispo["destination_complete"] == dest_selectionnee]
                        
                        if not match_base.empty:
                            client_reel = match_base.iloc[0]["client_nom"]
                            site_reel = match_base.iloc[0][nom_colonne_site]
                            zone_reelle = match_base.iloc[0]["zone"]
                        else:
                            client_reel = dest_selectionnee
                            site_reel = "Standard"
                            zone_reelle = "Zone A"
                        
                        # MATCHING DE DISPATCH AVEC LA ZONE RÉELLE DU SITE CLIENT
                        partenaires_zone = df_surplus_dispo[df_surplus_dispo["zone"] == zone_reelle]
                        
                        if not partenaires_zone.empty:
                            nom_partenaire = partenaires_zone.iloc[0]["client_nom"]
                            solution_transport = f"🔑 Flotte Client ({nom_partenaire})"
                            gain_prevu = nb_voyages * tonnage_std * (spot_ref * 0.15)
                            gain_text = f"{gain_prevu:,.0f} FCFA"
                        else:
                            solution_transport = "🔴 Sous-traitant Spot"
                            gain_prevu = 0
                            gain_text = "0 FCFA"
                            
                        suggestions.append({
                            "Date Prévue": date_str_sheet,
                            "Code Commande": code_auto,
                            "Client": client_reel,
                            "Site de Livraison": site_reel,
                            "Zone Logistique": zone_reelle,
                            "Volume": f"{vol_cmd} T",
                            "Voyages Requis": nb_voyages,
                            "Transporteur Assigné": solution_transport,
                            "Économie Générée": gain_text
                        })
                        
                        mises_a_jour_sheet.append([
                            date_str_sheet, code_auto, client_reel, site_reel, zone_reelle, vol_cmd, solution_transport, gain_prevu
                        ])

                    st.dataframe(pd.DataFrame(suggestions), use_container_width=True)
                    
                    # --- SAUVEGARDE SUR GOOGLE SHEETS ---
                    with st.spinner("💾 Enregistrement du planning dans Google Sheets..."):
                        try:
                            scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
                            if "gcp_service_account" in st.secrets:
                                creds_dict = dict(st.secrets["gcp_service_account"])
                                if "private_key" in creds_dict: 
                                    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
                                creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
                            else:
                                creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
                            
                            gc = gspread.authorize(creds)
                            wb = gc.open_by_key(SHEET_ID)
                            ws_livraisons = wb.worksheet("LIVRAISONS_JOUR")
                            
                            ws_livraisons.resize(rows=150)
                            ws_livraisons.update("A3", mises_a_jour_sheet)
                            st.success(f"✅ Planning multisite de {len(df_cmd)} lignes enregistré dans l'onglet LIVRAISONS_JOUR !")
                        except Exception as sheet_err:
                            st.error(f"Erreur lors de l'écriture sur Google Sheets : {sheet_err}")

                    # Alerte Capacités Silos Usine par Jour
                    df_res_voyages = pd.DataFrame(suggestions)
                    df_voyages_par_jour = df_res_voyages.groupby("Date Prévue")["Voyages Requis"].sum()
                    for jour_p, total_v in df_voyages_par_jour.items():
                        if total_v > 40:
                            st.error(f"🚨 **Alerte Surcharge Silos le {jour_p} :** {total_v} rotations planifiées. Risque d'attente prolongée sous la rampe pneumatique d'Abidjan.")
                        else:
                            st.caption(f"🟢 Journée du {jour_p} fluide : {total_v} rotations.")
                else:
                    st.warning("Veuillez renseigner au moins une commande valide.")
                    
except Exception as e:
    st.error(f"Erreur d'exécution globale de l'application : {e}")
