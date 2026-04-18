#pragma once
#include <functional>
#include <map>
#include <vector>
#include <mutex>
#include <cstdint>   
#include "CSlotMap.h"
#include "Account.h"

template <typename T_Topic, typename T_Payload>
class IPublisher : public IContractor {
public:
    using SubscriberAction = std::function<void(const T_Payload&)>;
    using HandleType = uint64_t;

    virtual ~IPublisher() = default;

    IPublisher& subscribe(Account& acc, T_Topic topic, SubscriberAction action, bool allowLoopback = false) {
        HandleType subId;
        {
            std::lock_guard<std::mutex> lock(m_pubMutex);
            subId = m_subscriptions[topic].insert({ std::move(action), acc.getOwner(), allowLoopback });
        }
        
        // Safely bind the unsubscribe action to the subscriber's Account lifecycle
        _addContractTo(acc, Contract([this, topic, subId]() { this->_onUnsubscribe(topic, subId); }));
        return *this;
    }

protected:
    void publish(T_Topic topic, const T_Payload& payload, void* originator = nullptr) {
        std::vector<SubscriptionInfo> members_copy;
        {
            std::lock_guard<std::mutex> lock(m_pubMutex);
            auto it = m_subscriptions.find(topic);
            if (it == m_subscriptions.end() || it->second.empty()) return;
            
            // Copy subscribers to avoid deadlocks in case a callback tries to subscribe/unsubscribe
            members_copy.reserve(it->second.size());
            members_copy.assign(it->second.begin(), it->second.end());
        } 
        
        for (const auto& sub : members_copy) {
            if (originator == nullptr || sub.identity != originator || sub.allowLoopback) {
                sub.action(payload);
            }
        }
    }

private:
    struct SubscriptionInfo { 
        SubscriberAction action; 
        void* identity; 
        bool allowLoopback; 
    };
    
    void _onUnsubscribe(T_Topic topic, HandleType id) {
        std::lock_guard<std::mutex> lock(m_pubMutex);
        auto it = m_subscriptions.find(topic);
        if (it != m_subscriptions.end()) {
            it->second.erase(id);
            
            // Remove the topic entirely if there are no more subscribers 
            // to prevent memory leaks from dynamically generated topics.
            if (it->second.empty()) {
                m_subscriptions.erase(it);
            }
        }
    }
    
    std::map<T_Topic, CSlotMap<SubscriptionInfo>> m_subscriptions;
    std::mutex m_pubMutex;
};

template <typename T_Topic, typename T_Payload>
class CPublisher : public IPublisher<T_Topic, T_Payload> {
public:
    void publish(T_Topic topic, const T_Payload& payload, void* originator = nullptr) {
        IPublisher<T_Topic, T_Payload>::publish(topic, payload, originator);
    }
};

#if 1 // NO_PAYLOAD_VERSION

template <typename T_Topic>
class IPublisher<T_Topic, void> : public IContractor {
public:
    using SubscriberAction = std::function<void()>;
    using HandleType = uint64_t;

    IPublisher& subscribe(Account& acc, T_Topic topic, SubscriberAction action, bool allowLoopback = false) {
        HandleType subId;
        { 
            std::lock_guard<std::mutex> lock(m_pubMutex); 
            subId = m_subscriptions[topic].insert({ std::move(action), acc.getOwner(), allowLoopback }); 
        }
        _addContractTo(acc, Contract([this, topic, subId]() { this->_onUnsubscribe(topic, subId); }));
        return *this;
    }

protected:
    void publish(T_Topic topic, void* originator = nullptr) {
        std::vector<SubscriptionInfo> members_copy;
        { 
            std::lock_guard<std::mutex> lock(m_pubMutex); 
            auto it = m_subscriptions.find(topic); 
            if (it == m_subscriptions.end() || it->second.empty()) return; 
            
            members_copy.reserve(it->second.size());
            members_copy.assign(it->second.begin(), it->second.end()); 
        }
        
        for (const auto& sub : members_copy) { 
            if (originator == nullptr || sub.identity != originator || sub.allowLoopback) {
                sub.action(); 
            }
        }
    }

private:
    struct SubscriptionInfo { 
        SubscriberAction action; 
        void* identity; 
        bool allowLoopback; 
    };
    
    void _onUnsubscribe(T_Topic topic, HandleType id) { 
        std::lock_guard<std::mutex> lock(m_pubMutex); 
        auto it = m_subscriptions.find(topic); 
        if (it != m_subscriptions.end()) {
            it->second.erase(id);
            
            if (it->second.empty()) {
                m_subscriptions.erase(it);
            }
        } 
    }
    
    std::map<T_Topic, CSlotMap<SubscriptionInfo>> m_subscriptions;
    std::mutex m_pubMutex;
};

template <typename T_Topic>
class CPublisher<T_Topic, void> : public IPublisher<T_Topic, void> {
public:
    void publish(T_Topic topic, void* originator = nullptr) { 
        IPublisher<T_Topic, void>::publish(topic, originator); 
    }
};
#endif